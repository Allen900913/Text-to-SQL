"""
Table Filter — 讓 LLM 從候選表裡「選」出這一題真正需要的表
====================================================================
為什麼需要它（固定 K 的死結）：

table_retriever 的相似度排序後取固定 K 張，實測 139 題按「實際需要幾張表」
拆開來看，同一個 K=4 同時犯了兩個相反的錯：

    實際需要   題數        給了幾張   浪費    +KMB 召回
      1 張     64 (46%)      5.0     5.0×     100%
      2 張     57 (41%)      4.7     2.4×      93%
      3 張      7            4.6     1.5×      86%
      4 張     11            4.9     1.2×      36%

對將近一半的題目慷慨五倍，對難題又根本不夠。K 開大改善下面惡化上面，
K 縮小反之 —— 沒有任何 K 值能同時解決，因為「需要幾張表」本來就因題而異，
而固定 K 是一個與題目無關的常數。

為什麼不是用動態門檻解（已實測否決）：
    r=0.85  召回 60.4%   r=0.75  召回 69.8%   K=6  召回 95.0%
餘弦分數根本沒有 elbow。更根本的原因是：相似度知道「這題關於客戶」，
但不知道「算總消費必須 JOIN orders」—— 那是**結構必要性**，不是文字相似度。
相似度在原理上看不到它，所以任何純粹在相似度分數上做的切法都救不了。
（同一個原因也解釋了為什麼往檢索文件加詞彙三次都讓召回下降 ——
  問題不在訊號不夠多，在缺一層推理。）

所以這一層補的就是那層推理：LLM 讀候選表的用途描述，判斷「回答這一題
邏輯上必須用到哪些表」。它會做相似度做不到的事 —— 問「各類別的營收」
時把問題裡一個字都沒提到的 order_items 選進來。

三段漏斗的分工：
    相似度  →  高召回，把候選收斂到 N 張        （目前 21 張表全進，見下）
    LLM     →  高精準，選出邏輯上必要的表
    KMB     →  補上把它們接起來的橋接表

139 題實測（+KMB 之後的召回 / 平均帶進 Prompt 的表數 / 淨度）：
    K=4 純相似度      91.4%   4.9 張   35.2%
    LLM 單獨          97.8%   2.0 張   92.1%
    LLM ∪ 相似度 #1   99.3%   2.1 張   90.2%   ← 採用
召回升 7.9 個百分點，同時表數少 57% —— 這兩件事本來是對立的（K 開大兩者
一起上升），能同時改善是因為換掉的不是參數而是「憑什麼決定要幾張表」。

為什麼還要跟相似度第 1 名取聯集（見 table_retriever.select_tables）：
LLM 偶爾會漏掉問題裡明講的主體（#91「商品種類數最多」漏 products、
#107「多少張訂單已送達」漏 orders）—— 那正是相似度最不會錯的部分。
兩者的錯誤型態互補，所以取聯集。實測只取第 1 名就夠：
    ∪ Top-1  召回 99.3% / 2.1 張 / 淨度 90.2%
    ∪ Top-2  召回 99.3% / 2.7 張 / 淨度 66.0%   （召回沒再上升，只是變胖）

目前 21 張表的 name + comment 全部只有 1,070 字元，比取 Top-20 再截斷還便宜，
所以候選階段實質上不篩。CANDIDATE_N 存在是為了資料表變多時仍然成立 ——
這一層的意義是「架構在 100 張表時不會爆」，不是「現在省 token」。

候選清單只給表註解，不給欄位。實測加上全部 144 個欄位名與欄位註解
（清單 1,070 → 4,195 字元）召回持平 98.6%，只是把漏掉的題目換了一批 ——
付四倍 token 買到雜訊。該補的是 TABLE_COMMENT 本身：#121「備貨天數最長的
商品」原本漏 product_suppliers，因為它的表註解只有「商品供貨關係」六個字，
而「備貨天數」四個字就寫在它的欄位註解裡。把表註解補好就找到了，而且表註解
同時餵給 DDL、檢索向量與這份清單，一次修好三個地方。

失敗一律退回相似度 Top-K：LLM 回不出合法 JSON、選了不存在的表、或整個
API 掛掉，都不該讓這一題答不出來。這一層是加分項，不是單點故障。
"""
import json
import os
import random
import re
import threading
import zlib
from concurrent.futures import ThreadPoolExecutor

from loguru import logger as log
from sqlalchemy import text

from langgraph_sql.config import MYSQL_URI, llm_filter
from langgraph_sql.utils.db_manager import get_db_manager
from langgraph_sql.utils.llm_retry import invoke_with_retry

# 候選表數上限 —— **固定值**。
#
# 這一層的目的就是「不管資料庫長到多大，送進 LLM 的表數有一個上限」。
# 讓上限隨表數成長，等於這一層沒有在做它被建立的那件事。
#
# 為什麼是 40：
#   · §7.2 量到 dense 要涵蓋約 2/3 的表才不漏（Top-14/21 = 99.3%、Top-16/21 = 100%）。
#   · 41 張表實測候選召回 @40 = **100%**，且 40 vs 41 的 A/B 買不到任何東西
#     （96.1%/96.8% vs 94.2%/96.1%，差距在同組態變異之內）。
#   · 40 張表的目錄約 2,400 字元。§8.9 量到目錄 1,365 字元時錨點召回 94.8%、
#     3,093 字元時 88.4% —— 40 張表落在還沒開始掉的那一側。
#
# 這三條都只在 ≤41 張表上成立。**它不是一個經得起擴表的常數，是一個
# 會在擴表時失效而且失效無聲的常數** —— 所以有下面那道護欄。
#
# 93 張表實測（2026-09-03，零 LLM，305 題）候選召回已經不是 100% 了：
#
#     N=20  96.7%（漏 10 題）   N=25  98.0%（漏 6）   N=30  98.4%（漏 5）
#     N=40  99.0%（漏  3 題）   N=93 100.0%
#
# 縮 N 是**硬損失** —— 候選層漏掉的表後面沒有任何一層救得回來。
# 但開了欄位提示（§2.7k）之後目錄從 3,096 漲到 5,568 字元，
# 遠超 §8.9 量過的範圍，所以「縮 N 換回目錄長度」變成一個值得量的交換。
# `CANDIDATE_N` 環境變數是為那個 A/B 開的，**預設值仍是 40**。
CANDIDATE_N = int(os.environ.get("CANDIDATE_N", "40"))


def get_candidate_n(n_tables: int | None = None) -> int:
    """這一題要送幾張候選表給 LLM。

    永遠是 CANDIDATE_N，只在資料庫比它還小時退讓（沒得篩）。

    ⚠️ 表數超過 CANDIDATE_N 之後，這一段就是整個漏斗的召回天花板，而它用的是
    三層裡唯一會隨規模退化的相似度。每次擴表都要用 `eval_retrieval.py --funnel`
    重新確認候選召回 —— **守門員是那個數字，不是這個常數**。
    """
    if n_tables is None:
        n_tables = len(get_table_briefs())
    return min(n_tables, CANDIDATE_N)


_TABLE_BRIEF_SQL = """
SELECT TABLE_NAME, TABLE_COMMENT
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME
"""

_briefs: dict[str, str] | None = None
_brief_lock = threading.Lock()

_SYSTEM_PROMPT = """你是資料庫查詢的表選擇器。使用者問了一個問題，你要從資料表清單裡選出「回答這個問題必須用到的表」。

判斷原則：
1. 問題裡沒有提到、但邏輯上非用不可的表也要選。例如問「各類別的營收」，
   字面上只有「類別」，但營收要從訂單明細算，所以訂單明細表也必須選。
2. 只是為了把兩張表接起來的中介表不用選，系統會自動補上路徑。
   你只要選「答案的資料實際存在哪幾張表裡」。
3. 語意接近但意義不同的表不要選。看過不等於買過、放進購物車不等於買過、
   物流狀態不等於訂單狀態 —— 問題問哪一件事就選哪一張表。
4. 不確定該不該選時就選進來。少選一張表會讓這題答不出來，多選一張只是多讀一點。

輸出格式：只輸出一個 JSON 陣列，每個元素是 {"table": "表名", "reason": "為什麼這題需要它"}。
reason 用一句話說明。不要輸出 JSON 以外的任何文字。"""

# 「先寫草稿再選表」——RSL-SQL 那條 backward linking 的廉價版（不多花一次呼叫）。
#
# 為什麼可能有用：直接問「要哪些表」時，模型答得出「客戶、訂單、訂單明細」就
# 覺得夠了；但真的動手寫 `WHERE p.name = '原子習慣'` 時，products 是躲不掉的。
# 漏斗逐題拆開來看，+KMB 之後仍失手的題有一整類是這個形狀（#21 書名住
# products.name、#62 單價住 products.price）——**LLM 選表時想不到，寫 SQL 時會發現**。
#
# ⚠️ **射程邊界：草稿只看得到表註解，看不到欄位。** 所以它吃得到「值住在哪張表」
# （表註解會說 products 是商品主檔），吃不到「屬性藏在第三張表的欄位註解裡」——
# #127「已經停產」寫在 product_specs 的欄位註解上，草稿無從得知。那一類要的是
# 完整版（多一次呼叫、餵候選 40 張表的 DDL），成本完全不同，不在這個旋鈕裡。
#
# 預設關閉，用環境變數開：FILTER_DRAFT=1
_SYSTEM_PROMPT_DRAFT = _SYSTEM_PROMPT.replace(
    '輸出格式：只輸出一個 JSON 陣列',
    """先做一件事再輸出：用一兩行寫出你會怎麼查的 SQL 草稿。草稿不必正確、不必能執行，
目的是逼你把「這題實際要碰哪些資料」寫出來 —— 特別是問題裡出現的具體名稱、
數值條件要拿去比對哪一張表的哪個欄位。寫完草稿，再回頭看你的草稿碰到了哪些表。

輸出格式：先寫草稿，然後輸出一個 JSON 陣列""")

FILTER_DRAFT = os.environ.get("FILTER_DRAFT", "0") == "1"


def _system_prompt() -> str:
    """這一題用哪一份 system prompt。預設是原本那份，位元相同。"""
    return _SYSTEM_PROMPT_DRAFT if FILTER_DRAFT else _SYSTEM_PROMPT

_USER_TEMPLATE = """可用的資料表：
{catalog}

使用者問題：{query}

選出回答這個問題必須用到的表。"""


# 「關係宣告」句：註解裡點名另一張表的那一句，例如
#     shipment_profiles「…出貨倉庫與到貨時間在 shipments，每一次上門派送在 delivery_attempts」
# 全庫有 38 句這種話，其中 13 句在 *_profiles 上。
_REL_SENT = re.compile(r"[。;；]|——")
_REL_NAMES = re.compile(r"在 ([a-z_]{3,})")

# 只對寬表生效 —— 這是**被量到的範圍**。另外 25 句落在非寬表上，未量測。
_REL_SCOPE = "_profiles"

# **預設 keep = 關閉**（2026-08-26 全庫六輪配對驗收，見 _strip_relations()）。
# 用環境變數開，才能在**不改程式碼**的前提下跑 A/B ——
# 改常數再跑一次會讓兩次量測落在不同的 commit 上，事後分不清差異來自哪裡。
#     BRIEF_RELATIONS=strip python eval/eval_retrieval.py --funnel
_STRIP_RELATIONS = os.environ.get("BRIEF_RELATIONS", "keep") == "strip"


def _strip_relations(brief: str, known: set[str]) -> str:
    """丟掉點名了另一張表的句子。**這是分欄，不是刪除**（ARCHITECTURE §2.5）。

    TABLE_COMMENT 仍然是唯一權威來源，也仍然原封不動進 DDL —— `get_ddl()` 走的是
    `semantic_layer.yaml`（由 tools/gen_ddl.py 從 information_schema 產生），
    跟這裡完全是兩條路。**這個函式只影響檢索文件與 LLM 選表目錄。**

    **判決：平手，不上線（2026-08-26，全庫 305 題 × 各 3 輪 × 逐題配對）。**

        anchor  變好 14  變差 14  持平 277   符號檢定 p = 0.5747
        kmb     變好  9  變差  9  持平 287   符號檢定 p = 0.5927

    有一輪撞 429 降級 59 題，拿掉之後結論不變（p = 0.40 / 0.50）；
    只取乾淨的第 1、3 輪也一樣（p = 0.30 / 0.50）。**14 比 14、9 比 9。**

    ⚠️ **單輪全庫漏斗沒有解析度，別再用它下判斷。** 同一個設定重複執行：

        錨點召回   拆欄臂 n=10   93.4 ~ 96.1%     ← 區間 2.7pp
                   現行臂 n=5    93.4 ~ 94.8%     ← 區間 1.4pp

    這條路上量到的「+1.0pp」（單輪 A 94.1 → C' 95.1）整個泡在裡面。
    §5.2 的「單輪評估分不出差異」只對 e2e 寫過，**漏斗也一樣，而且我今天
    靠單輪漏斗下了三個判斷**（B 首句 −2.0pp、C' +1.0pp、「A 自己 spread 只有 0.3pp」）
    —— 最後那句是拿兩個樣本當區間，錯得最離譜。

    機制：那句話寫在**正解寬表**上時是在說「主要的東西在母表」，模型就跑去選母表。
    §2.5 量過它的鏡像 ——「往干擾表加指路標沒有用」（`payment_attempts` 的註解
    寫著「成功的付款結果在 payments」而完全無效）。**指路標只在指離正解時起作用。**

    ⚠️ **它不是單向的。** 12 題探針上三組全贏（誘餌 3/32→9/32、窄表 34/40→40/40），
    但全庫一跑就有 4 題退步，而且機制乾淨：`#286`/`#287` 需要
    `review_profiles` **和** `reviews` 兩張，而被丟掉的正是
    「星等與評價文字在 reviews」—— **那句話本來就在告訴模型「你還需要母表」**。

        指路標在   → 跑去母表，忘了寬表     #282 #292
        指路標不在 → 留在寬表，忘了母表     #286 #287

    所以這仍然是 §2.7g 那條軸，只是粒度變了：不是「寬表 vs 窄表」，
    是**「這一題需要幾張表」**。而配對之後兩端**恰好相抵** —— 這不是巧合，
    是同一個機制的兩面，跟欄位提示（§2.7f）與第 5 條原則（§2.7g）同一個結局。

    程式碼留著的理由：它是**分欄**這個做法的可運行紀錄。TABLE_COMMENT 一段文字
    同時當檢索文件、選表目錄與 DDL 註解，三個職務的最佳內容並不相同；
    這個函式證明了「拆給不同職務看」在實作上是零成本的（DDL 走
    semantic_layer.yaml，結構上不受影響）。**沒兌現的是收益，不是做法。**
    """
    keep = []
    for part in _REL_SENT.split(brief):
        part = part.strip()
        if not part:
            continue
        if any(w in known for w in _REL_NAMES.findall(part)):
            continue
        keep.append(part)
    return "。".join(keep)


def get_table_briefs() -> dict[str, str]:
    """{表名: 表註解}。給 LLM 看的候選清單，只有註解，不含欄位。

    ⚠️ 回傳的**不是**原始 TABLE_COMMENT：寬表的「關係宣告」句已經被
    `_strip_relations()` 拆掉（見該函式）。DDL 拿到的仍然是完整註解。
    """
    global _briefs
    if _briefs is not None:
        return _briefs
    with _brief_lock:
        if _briefs is not None:
            return _briefs
        db = get_db_manager(MYSQL_URI)
        with db.engine.connect() as conn:
            rows = conn.execute(text(_TABLE_BRIEF_SQL)).fetchall()
        _briefs = {t.lower(): (c or "").strip() for t, c in rows}
        if _STRIP_RELATIONS:
            known = set(_briefs)
            _briefs = {t: (_strip_relations(b, known) if t.endswith(_REL_SCOPE) else b)
                       for t, b in _briefs.items()}
        _warn_if_candidate_n_binds(len(_briefs))
        return _briefs


def _warn_if_candidate_n_binds(n_tables: int) -> None:
    """表數超過 CANDIDATE_N 時大聲說出來 —— 這個天花板效應是無聲的。

    表 <= CANDIDATE_N 時第一段完全不篩，漏斗的召回上限是 100%。一旦超過，
    第一段變成關鍵路徑，而它用的是三層裡**唯一會隨規模退化**的相似度
    （實測 −0.44pp/表，真實近義干擾表）。更麻煩的是那一段漏掉的表，
    後面兩段再準都救不回來（§7.2）—— 分數會掉，但沒有任何東西會報錯。

    護欄不擋動作，只保證這件事不會安靜地發生。
    """
    if n_tables <= CANDIDATE_N:
        return
    briefs = get_table_briefs()
    per = (sum(len(t) + len(c) + 4 for t, c in briefs.items()) // len(briefs)) if briefs else 61
    log.warning(
        f"[Filter] 資料庫有 {n_tables} 張表 > 候選上限 {CANDIDATE_N} —— "
        f"相似度那一段開始真的淘汰表（每題砍掉 {n_tables - CANDIDATE_N} 張，"
        f"候選只涵蓋 {CANDIDATE_N / n_tables:.0%}），它現在是整個漏斗的召回天花板，"
        f"而且掉了不會報錯。候選目錄約 {CANDIDATE_N * per:,} 字元。"
        f"請跑 `python eval/eval_retrieval.py --funnel` 確認候選召回 —— "
        f"守門員是那個數字，不是 CANDIDATE_N 這個常數（依據見 ARCHITECTURE.md §7.2）"
    )


def format_catalog(tables: list[str], shuffle_seed: int | None = None,
                   query: str | None = None) -> str:
    """候選清單。

    ⚠️ shuffle_seed=None（照相似度排名）是**舊行為，已被實測否決**。
    原本的理由是「高分在前，等於免費給 LLM 一個先驗」——
    實測（ARCHITECTURE.md §7.12）那個先驗是**淨負的**：

        固定順序 n=3   錨點召回 94.8 / 92.9 / 93.5   +KMB 97.4 / 96.8 / 97.4
        打散順序 n=2   錨點召回 98.1 / 96.1          +KMB 99.4 / 98.1

    兩組區間完全不重疊。位置偏誤影響的不只是「排後面被忽略」，
    固定順序還會讓同一組偏誤每次都重現，於是錯的那幾題**穩定地錯**。

    production 走的是 table_retriever，它用問題的 CRC32 當 seed ——
    同一題永遠同一種順序（可重現、可除錯），不同題順序不同（不固化成新偏誤）。
    這裡保留 None 是為了讓評估程式能跑「固定順序」這個對照組。

    `query` 給了才會加「本題可能相關的欄位」那一行（§2.7f）。預設 None ——
    評估程式要跑「沒有欄位提示」的對照臂時不必改任何東西。
    """
    briefs = get_table_briefs()
    if shuffle_seed is not None:
        tables = list(tables)
        random.Random(shuffle_seed).shuffle(tables)
    hints, evid = {}, {}
    if query:
        # 在函式內 import：column_hints → embedding → config 這條鏈與 table_filter
        # 無關，但 table_filter 是 table_retriever 的相依，放頂層會讓相依圖更難讀。
        from langgraph_sql.utils.column_hints import hints_for
        from langgraph_sql.utils.value_index import evidence_for
        hints = hints_for(query, list(tables))
        # 值命中的**事實**（§2.7n）：問句裡的哪個字串，是本表哪個欄位的值。
        # 與 hints 的差別是「查到的」與「猜的」—— hints 用餘弦挑欄位，
        # 每題每表都要付文字；證據只在真的命中時出現（實測 13/305 題），
        # 所以它踩不到 §8.9 那條「目錄變長 → 錨點召回下降」的曲線。
        evid = evidence_for(query, list(tables))
    return "\n".join(f"- {t}: {briefs.get(t, '')}{hints.get(t, '')}{evid.get(t, '')}"
                     for t in tables)


def _json_arrays(raw: str) -> list[list]:
    """
    抽出字串裡所有能成功解析的 JSON 陣列，依出現順序。

    用 raw_decode 從每個 '[' 試著往下解，而不是用 regex 抓 `\\[.*?\\]` ——
    reason 是模型自由書寫的中文，裡面出現一個 ']' 就會讓 regex 提早收尾，
    而且那種壞法是靜默的（解析失敗 → 回空 → 安靜退回相似度）。
    JSON 解析器本來就懂字串跳脫，交給它做。
    """
    decoder = json.JSONDecoder()
    found: list[list] = []
    for i, ch in enumerate(raw):
        if ch != "[":
            continue
        try:
            value, _ = decoder.raw_decode(raw, i)
        except ValueError:
            continue
        if isinstance(value, list):
            found.append(value)
    return found


def _parse_selection(raw: str, allowed: set[str]) -> list[str]:
    """
    從模型輸出抽出表名。

    只認 allowed 裡的表 —— 模型偶爾會回傳它自己想像的表名，那種東西放行下去
    會讓 KMB 拿一個圖上不存在的節點去找路徑。取最後一個有效的陣列，因為
    reasoning 模型有時會先寫一段草稿陣列再給結論。
    """
    for items in reversed(_json_arrays(raw)):
        picked: list[str] = []
        for item in items:
            name = item.get("table") if isinstance(item, dict) else item
            if not isinstance(name, str):
                continue
            name = name.strip().lower()
            if name in allowed and name not in picked:
                picked.append(name)
        if picked:
            return picked
    return []


# 選表要投幾票。**預設 3 = 聯集投票（2026-08-28 通過 e2e 驗收後上線，見 §2.7i）**。
# 設回 1 就是位元相同的舊行為（votes=1 走的是與原本完全相同的那一顆 seed），
# 對照臂契約由 scratchpad/pool_check.py 的 PC4 守住：
#
#     FILTER_VOTES=1 python eval/eval_retrieval.py --funnel   # 舊行為（對照）
#
# 為什麼是聯集不是多數決：這一層的錯是**單向**的 —— 漏一張表這題就死了，
# 多一張表只是 Prompt 長一點。§7.x 逐題拆開看，+KMB 之後仍失手的題分成
# 「三輪都漏同一張」（穩定）與「三輪漏一次兩次」（硬幣）兩群，聯集只動得了
# 後者。多數決會把硬幣題的少數正確票丟掉，剛好丟掉唯一有價值的那一票。
#
# 上線的帳（§2.7i）：漏斗 +KMB 97.1% → 98.5%（9 救 0 丟，p=0.0020）、
# e2e 逐題配對 7 救 0 丟（p=0.0156，只算兩臂表集合真的不同的 173 組配對）、
# 防禦題 6 格全部 4/4。代價是**選表呼叫 1 → 3 次/題**（三票平行，延遲不變，
# 但配額吃三倍）與**進 Prompt 的表 2.35 → 2.57 張（+9.4%）**。
FILTER_VOTES = int(os.environ.get("FILTER_VOTES", "3"))
VOTE_POOL = int(os.environ.get("FILTER_VOTE_POOL", "0"))  # 0=每票一執行緒；1=序列

# 死票計數 —— **這道護欄是拿一場災難換來的。**
# few-shot 那次全庫兩臂比較，B 臂第 2 輪撞上 rate limit，288/309 題整個掛掉，
# 而過程中沒有任何東西喊停；要不是總分低到不可能，那一輪會被當成有效資料。
# 投票制把這個風險放大三倍：一票死掉時 filter_tables_union 仍然回得出東西，
# B 臂會**安靜地退化成 A 臂**，而退化的方向剛好是「效果變小」——
# 也就是說限流會偽裝成「這個改動沒有用」。所以要能事後查驗有多少票是死的。
VOTE_TALLY = {"cast": 0, "dead": 0, "questions": 0, "degraded": 0}
_tally_lock = threading.Lock()


def reset_vote_tally() -> dict:
    """取出並歸零。評估程式每一輪呼叫一次，把死票率跟那一輪的分數存在一起。"""
    global VOTE_TALLY
    with _tally_lock:
        old, VOTE_TALLY = dict(VOTE_TALLY), {"cast": 0, "dead": 0, "questions": 0, "degraded": 0}
    return old


def _vote_seeds(query: str, votes: int) -> list[int]:
    """每一票一顆 seed。**第 0 顆必須等於原本的 crc32(query)。**

    production 原本用 `crc32(query)` 當打散 seed（§7.12：固定順序 vs 打散，
    兩組區間不重疊）。同一顆 seed 重跑只會拿到 MoE 那一點分歧；換 seed 會換
    整個候選順序，也就換掉位置偏誤的落點 —— 那才是這幾票該有的獨立性來源。
    """
    return [zlib.crc32((query if i == 0 else f"{query}#{i}").encode("utf-8"))
            for i in range(votes)]


def filter_tables_union(query: str, candidates: list[str],
                        votes: int | None = None) -> list[str]:
    """跑 `votes` 次選表，取**聯集**。votes=1 時等同直接呼叫 filter_tables。

    降級契約不變：某一票掛掉就當它沒投，其餘照算；全部掛掉才回空陣列，
    呼叫端仍然退回相似度。**一票失敗不會讓這一題答不出來。**
    """
    votes = FILTER_VOTES if votes is None else votes
    seeds = _vote_seeds(query, max(1, votes))

    # VOTE_POOL<=0：每票一條執行緒（現行）。設成 1 就是序列投票。
    # 加這個旋鈕是因為 2026-08-27 那次漏斗 A/B 被 429 打爛：votes=3 × workers=2
    # 等於 6 個並發選表呼叫，再加重試，配額直接見底。concurrency 不是限流的
    # 成因（RPM 才是），但序列化是唯一不必先知道 RPM 上限就能壓住它的手段。
    pool_n = len(seeds) if VOTE_POOL <= 0 else min(VOTE_POOL, len(seeds))
    if pool_n <= 1:
        ballots = [filter_tables(query, candidates, shuffle_seed=sd) for sd in seeds]
    else:
        with ThreadPoolExecutor(max_workers=pool_n) as pool:
            ballots = list(pool.map(
                lambda sd: filter_tables(query, candidates, shuffle_seed=sd), seeds))

    merged: list[str] = []
    for b in ballots:
        for t in b:
            if t not in merged:
                merged.append(t)
    dead = sum(1 for b in ballots if not b)
    if dead and len(seeds) > 1:
        log.warning(f"[Filter] {len(seeds)} 票裡有 {dead} 票失效，用剩下的取聯集")
    with _tally_lock:
        VOTE_TALLY["cast"] += len(seeds)
        VOTE_TALLY["dead"] += dead
        VOTE_TALLY["questions"] += 1
        if dead:
            VOTE_TALLY["degraded"] += 1
    return merged


def filter_tables(query: str, candidates: list[str],
                  shuffle_seed: int | None = None) -> list[str]:
    """
    從 candidates 選出這一題需要的表。回傳空陣列代表這一層沒作用，
    呼叫端應退回相似度的結果 —— 不要把空陣列當成「這題不需要任何表」。
    """
    if not candidates:
        return []

    allowed = {t.lower() for t in candidates}
    content, error = invoke_with_retry(
        llm_filter,
        [{"role": "system", "content": _system_prompt()},
         {"role": "user", "content": _USER_TEMPLATE.format(
             catalog=format_catalog(candidates, shuffle_seed, query=query),
             query=query)}],
        tag="[Filter]",
    )
    if error:
        log.warning(f"[Filter] LLM 呼叫失敗（{error}），退回相似度排名")
        return []

    picked = _parse_selection(content, allowed)
    if not picked:
        log.warning(f"[Filter] 無法從回覆解析出合法表名，退回相似度排名: {content[:200]}")
    return picked
