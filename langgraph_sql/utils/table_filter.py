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
import random
import threading

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
CANDIDATE_N = 40


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

_USER_TEMPLATE = """可用的資料表：
{catalog}

使用者問題：{query}

選出回答這個問題必須用到的表。"""


def get_table_briefs() -> dict[str, str]:
    """{表名: 表註解}。給 LLM 看的候選清單，只有註解，不含欄位。"""
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


def format_catalog(tables: list[str], shuffle_seed: int | None = None) -> str:
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
    """
    briefs = get_table_briefs()
    if shuffle_seed is not None:
        tables = list(tables)
        random.Random(shuffle_seed).shuffle(tables)
    return "\n".join(f"- {t}: {briefs.get(t, '')}" for t in tables)


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
        [{"role": "system", "content": _SYSTEM_PROMPT},
         {"role": "user", "content": _USER_TEMPLATE.format(
             catalog=format_catalog(candidates, shuffle_seed), query=query)}],
        tag="[Filter]",
    )
    if error:
        log.warning(f"[Filter] LLM 呼叫失敗（{error}），退回相似度排名")
        return []

    picked = _parse_selection(content, allowed)
    if not picked:
        log.warning(f"[Filter] 無法從回覆解析出合法表名，退回相似度排名: {content[:200]}")
    return picked
