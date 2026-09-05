# -*- coding: utf-8 -*-
"""候選目錄裡的「本題可能相關的欄位」那一行（ARCHITECTURE.md §2.7f）。

**為什麼需要這個。** `get_table_briefs()` 的 docstring 自己寫著「給 LLM 看的
候選清單，只有註解，不含欄位」。於是選表那一步得從一句散文去猜
`shipment_profiles` 裡有沒有「中途改地址」這個欄位。而全庫 1071 欄
**100% 都有註解**，且註解與問句常常幾乎逐字對應：

    #282「中途改過地址」 ← shipment_profiles.is_redirected「是否中途改過送件地址」
    #292「跟對帳單勾稽」 ← payment_profiles.is_reconciled「是否已與金流對帳單勾稽」

實測（零 LLM）：八題裡七題，正解欄位在全庫 869 欄中排 1~2 名，
而且都是**自己表內的第 1 名** —— 所以 `HINT_K = 2` 就夠，不用列一長串。

**這一層加容量是安全的，加在檢索層不安全**（§2.7b）：
檢索層是 N 份文件取 max，拆越多份，寬表被選中的機會越多（寬表誤選 6 → 21）；
候選目錄裡每張表只列一次，**不存在取最大值**，所以目錄只會變長，不會變偏。
實測 10 題純窄表題，一次都沒有把 `*_profiles` 拉進答案。

**還有一個對照臂的教訓（§2.7f 的 F 臂）。** 固定列「最有辨識度」的欄位、
不做相關性主張、同樣加兩欄文字 —— 誘餌題是 **0/32**，還把三題推得更差。
**收益不是來自「多了欄位文字」，是來自「挑對了欄位」。**
所以這裡的欄位一定要依問句挑，不能改成任何與問句無關的固定清單。

失敗時一律安靜退回「沒有這一行」，不讓這個增強變成新的單點故障。
"""
import json
import os
import threading

from loguru import logger as log
from sqlalchemy import text

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager
from langgraph_sql.utils.embedding import cosine, doc_hash, embed, embed_query

# 每張表在目錄裡列幾個欄位。**2026-09-05 起預設 2 = 開啟**（§9.8、§9.9）。
#
# ---------------------------------------------------------------------------
# 這個預設值被否決過六次，第七次才通過。理由要看清楚，否則會誤讀成「當初判錯了」
# ---------------------------------------------------------------------------
#
# 舊的否決理由（2026-08-26 全庫驗收，§2.7f）：全庫 305 題一跑就是平手，
# 而且是**雙向**的 —— n=8 複驗 `#282` 1/8 → 8/8、`#308` 2/8 → 7/8，
# 但 `#94`、`#100` 是 **8/8 → 0/8**。當時的結論是
# 「修好與弄壞來自同一個機制，不存在只留好處的參數設定」（見 hints_for()）。
#
# **2026-09-05 推翻它的不是新論證，是新量測。** NIM 在 2026-09-03 18:33
# 無聲換過服務端模型，於是重量了一次（判準事前登記在 commit effa5b5）：
#
#     A（K=0）三輪  96.8 ~ 97.1%     穩定錯 4 題   硬幣池 14 題
#     B（K=2）三輪  98.1 ~ 98.7%     穩定錯 1 題   硬幣池  9 題
#     → **B 臂最差一輪贏過 A 臂最好一輪，六輪完全不重疊**；防禦題六輪全 4/4
#
# 關鍵是那個雙向性**消失了**：事前登記的陷阱家族
# `#94` `#100` `#49` `#36` `#146` `#147` **兩臂全部 3/3，一格沒掉**。
# 零 LLM 探針確認提示的文字一個字沒變（`#100` 的第一名候選仍是
# `customer_profiles`、第一欄仍是 `total_spent（累計消費金額（每日結算快照…）`）——
# **所以改變的是模型讀提示的方式，不是提示本身**（[[baselines-die-when-the-model-changes]]）。
#
# 收益的機制查清楚了，有兩條，第二條是原本沒預期到的：
#     召回   `#292` 0/3 → 3/3。選表實測：A 選 [invoices, payments]（漏表），
#            B 選 [payment_profiles, payments] —— 提示是
#            `is_reconciled（是否已與金流對帳單勾稽）`，與問句幾乎逐字對應。
#     精準   `#167`：A 選 [order_items, order_returns] 把「購買數量」加總得 8.0；
#            B 看到 `order_returns ▸ quantity（退回數量）` 之後**只選 order_returns**。
#            **提示也會讓它丟掉近義干擾表**，不是只會多給表。
#
# ⚠️ 六輪裡 B 臂有 6 題比 A 差（`#287` −2，`#53` `#83` `#97` `#225` `#262` 各 −1）。
# **這些是 n=3 的逐題差異，依 §5.2 不構成證據**，判決建立在輪次區間上。
# `#287` 是其中最大的一個，值得單獨 n=8 追一次，還沒做。
#
# 成本：目錄 3,096 → 5,568 字元（+2,472／題），平均延遲約 +2s。
# 這條曲線 §2.7l 量過會咬人，所以 K 不要再往上調 —— 實測正解欄位在自己表內
# 幾乎都是第 1 名，`K = 2` 就夠。
#
# 用環境變數覆寫，才能在**不改程式碼**的前提下跑 A/B ——
# 改常數再跑一次會讓兩次量測落在不同的 commit 上，事後分不清差異來自哪裡。
#     COLUMN_HINT_K=0 python eval/test_runner.py     # 位元還原到 2026-09-04 的對照臂
HINT_K = int(os.environ.get("COLUMN_HINT_K", "2"))

# 沿用 table_retriever 的慣例：專案根目錄的點檔，key 是文件雜湊。
# **不要**叫 .column_vectors.json —— 那個名字已經被 eval_column_recall.py 佔用。
_CACHE_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), ".column_hint_vectors.json")

_docs: dict[tuple[str, str], str] | None = None
_comments: dict[tuple[str, str], str] | None = None
_vectors: dict[tuple[str, str], list[float]] | None = None
_lock = threading.Lock()

# 主鍵與外鍵不帶語意，列進目錄只會製造雜訊（每張表都有 id、xxx_id）。
_SKIP = ("id",)


def _load() -> tuple[dict, dict]:
    """建立每個欄位的檢索文件與註解表。

    文件格式：`{表註解的前 18 字} · {欄位名}（{欄位註解}）`

    為什麼要帶表註解的前段：`is_active`、`created_at` 這種欄位光看註解在 93 張表
    裡是重複的，不帶表的脈絡就分不出是誰的。只取 18 字，避免整段表註解
    把欄位註解稀釋掉（§2.7 稀釋定律形式 ①）。
    """
    global _docs, _comments
    if _docs is not None and _comments is not None:
        return _docs, _comments
    with _lock:
        if _docs is not None and _comments is not None:
            return _docs, _comments
        with get_db_manager(MYSQL_URI).engine.connect() as conn:
            db = conn.execute(text("SELECT DATABASE()")).scalar()
            briefs = {t.lower(): (c or "") for t, c in conn.execute(text(
                "SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = :d"), {"d": db})}
            rows = conn.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
                "FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = :d "
                "ORDER BY TABLE_NAME, ORDINAL_POSITION"), {"d": db}).fetchall()
        docs, cmts = {}, {}
        for t, col, cm in rows:
            t = t.lower()
            if col in _SKIP or col.endswith("_id"):
                continue
            cm = (cm or "").strip()
            if not cm:
                continue                      # 沒註解的欄位沒有可比對的語意
            head = briefs.get(t, "").split("：")[0][:18]
            docs[(t, col)] = f"{head} · {col}（{cm}）"
            cmts[(t, col)] = cm
        _docs, _comments = docs, cmts
        log.info(f"[ColumnHints] 欄位文件 {len(docs)} 份（已排除主鍵外鍵與無註解欄位）")
        return _docs, _comments


def get_column_vectors() -> dict[tuple[str, str], list[float]]:
    """欄位向量，帶磁碟快取。key 是文件雜湊，所以改了欄位註解會自動失效。"""
    global _vectors
    if _vectors is not None:
        return _vectors
    # _load() 必須在拿 _lock **之前**呼叫 —— 它自己也會拿同一把鎖，
    # 而 threading.Lock 不可重入，巢狀就是死結（實測掛住，不會報錯）。
    docs, _ = _load()
    with _lock:
        if _vectors is not None:
            return _vectors
        cache: dict[str, list[float]] = {}
        if os.path.exists(_CACHE_PATH):
            try:
                with open(_CACHE_PATH, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception as e:
                log.warning(f"[ColumnHints] 快取讀取失敗（{type(e).__name__}），重新計算")
        keys = sorted(docs)
        stale = [k for k in keys if doc_hash(docs[k]) not in cache]
        if stale:
            log.info(f"[ColumnHints] 需要嵌入 {len(stale)} 個欄位的描述")
            for i in range(0, len(stale), 96):
                chunk = stale[i:i + 96]
                for k, v in zip(chunk, embed([docs[k] for k in chunk], "passage")):
                    cache[doc_hash(docs[k])] = v
            try:
                os.makedirs(os.path.dirname(_CACHE_PATH), exist_ok=True)
                with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
            except Exception as e:
                log.warning(f"[ColumnHints] 快取寫入失敗（{type(e).__name__}），下次會重算")
        _vectors = {k: cache[doc_hash(docs[k])] for k in keys}
        return _vectors


def hints_for(query: str, tables: list[str], k: int = HINT_K) -> dict[str, str]:
    """{表名: 要接在目錄那一行後面的字串}。任何一步失敗都回空 dict。

    **為什麼這個功能預設關閉 —— 失敗機制（2026-08-26）**

    `#94`「找出消費金額高於所在城市平均消費的客戶」8/8 → 0/8。查下去是這樣：

        customer_profiles  0.302  total_spent（累計消費金額（每日結算快照，
                                  不含最近數日訂單，與即時 SUM(orders.total_amount) 可能不同））
        customers          0.239  city（居住城市）、name（客戶姓名）
        orders             0.131  total_amount（訂單總金額）

    模型看到「累計消費金額」就一張表答完，不 JOIN `orders` —— 而
    `customer_profiles.total_spent` 正是 `seed_customer_profiles` 刻意埋的
    **快照落後陷阱**（50 位客戶有 27 位與即時值對不上，閘門 [9] 已宣告為刻意）。

    > **欄位提示會把「刻意的陷阱」變成「明顯的捷徑」。**

    注意那段註解**自己就寫著**「與即時 SUM(orders.total_amount) 可能不同」，
    模型照樣走捷徑 —— 所以這不是「警語寫得不夠」，加更多字沒有用。
    門檻也切不開：0.302（錯）vs 0.239（對），差距比雜訊還小。

    而修好的那幾題是同一個機制的另一面：寬表**就是**正解時，提示直接命中
    （`#282` 的 `is_redirected`、`#308` 的 `is_over_policy_window`）。
    **修好與弄壞來自同一個機制**，所以不存在「只留好處」的參數設定。
    """
    if k <= 0 or not tables:
        return {}
    try:
        vecs = get_column_vectors()
        _, cmts = _load()
        qvec = embed_query(query)
    except Exception as e:
        log.warning(f"[ColumnHints] 取欄位提示失敗（{type(e).__name__}: {e}），退回純表註解")
        return {}

    by_table: dict[str, list[tuple[str, str]]] = {}
    for key in vecs:
        by_table.setdefault(key[0], []).append(key)

    out = {}
    for t in tables:
        cols = by_table.get(t.lower())
        if not cols:
            continue
        # 分數並列時以欄位名排序，確保同一個問題每次得到同一組提示
        top = sorted(cols, key=lambda c: (-cosine(qvec, vecs[c]), c[1]))[:k]
        out[t] = "\n    ▸ 本題可能相關的欄位：" + "、".join(
            f"{c}（{cmts[(tt, c)]}）" for tt, c in top)
    return out
