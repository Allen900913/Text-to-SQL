# -*- coding: utf-8 -*-
"""值索引 —— 問句點名了儲存格裡的「值」時，資料庫自己知道它住在哪（§2.7n）

**要解的是哪一種失敗。** 三段漏斗的每一層都靠文字：dense 比對表註解、
LLM 讀表註解、KMB 走外鍵。但有一整類問句給的既不是概念也不是結構，
而是**一個實際存在的值**：

    #21「從來沒有買過『原子習慣』這本書的客戶有誰？」
    #40「買過『iPhone 15』但沒買過『AirPods』的客戶」

嵌入不知道「iPhone 15」是商品 —— 那五個字跟「商品資訊表」沒有語意相似。
**但資料庫知道**，因為它就是 `products.name` 裡的一個值。§2.7 量到
`#21` 的 products 排到第 52 名、`#40` 第 43 名，改表註解最多只能救到第 19 名。

這一支就是那條缺掉的通道。它**不取代任何一層** —— 全庫 305 題裡只有
四十分之一有值可命中，其餘九成靠的還是 dense 與 LLM 推理。

兩個接點，職責不同（§2.7n）：

    dense 層      rank_tables() 把 β·min(命中數, 3) 加在餘弦上 → 把表撈進候選
    LLM 選表層    format_catalog() 在候選目錄那一行後面附事實 → 讓它知道值住在哪

**目錄那一行只報事實，不報建議。** 這是實測逼出來的要求：
「書籍」同時是 `categories.name` 與 `products.category` 的值，「運動鞋」
同時在 `products.name` 與 `search_logs.keyword` 裡 —— 兩邊都是真的。
把命中位置**全部列出來**讓 LLM 自己選，才是「不被騙」；只挑一個報就變成
§2.7f 的 F 臂（有主張的錯，誘餌題 0/32）。

**為什麼刪掉基數上限。** 原型（`eval/eval_retrieval_variants.py`）只收
相異值 ≤ 60 的欄位。實測那是一道無聲的懸崖：`products.name` 今天 40 個值，
商品目錄長到 61 項整條通道就消失、而且不會報錯。更荒謬的是它排除掉的正是
`invoice_no`(164)、`tracking_no`(143)、`barcode`(148) —— 值檢索在業界最主要
的用途。噪音不該用基數擋，該用**值本身像不像實體名**擋（見 `_keep`）。

**為什麼不做磁碟快取。** 建索引 5.3 秒，而快取要正確就得有一個能偵測
「資料被 UPDATE 過」的指紋 —— 列數與 schema 註解都抓不到就地修改。
一個會無聲失效的快取比沒有快取糟（[[silent-pass-is-not-a-pass]]）。
行程內記憶一次就夠：評估與 pipeline 都是單行程長時間跑。

**2026-09-04 起預設全開**（`VALUE_BETA=0.05`、`VALUE_EVIDENCE=1`）。

採用的依據**不是 e2e 分數**：兩臂各三輪，區間重疊，事前登記的判準判平手
（§2.7n）。依據是零 LLM 那組硬指標 —— 候選@40 與對照臂同為 305/305，
但不必付 `products` 表註解那句泛稱的漏出代價；閘門 [12] 的 `products`
最差名次 62 → 33。**買的是斜率不是截距**：今天 93 張表撈得到的，
明天 200 張表撈不到。

開關仍留在環境變數上：`VALUE_BETA=0 VALUE_EVIDENCE=0` 可還原成與這支
存在之前位元相同的行為 —— A/B 兩臂要落得進同一個 commit。
"""
import os
import threading

from loguru import logger as log
from sqlalchemy import text

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager

# dense 層的加權。**2026-09-04 起預設 0.05 = 開啟**；設成 0 可完全還原。
#
# 0.05 是 §2.7 掃出來的曲線上的點（β=0.02/0.05/0.10 三點，0.05 與 0.10 同分），
# **不是調出來的旋鈕**。要動它請重掃整條曲線，不要單點微調（§10「停止調常數」）。
# 值訊號怎麼進到 dense 這一層。兩個臂放在同一個 commit 裡，一位元可逆。
#   beta （預設，現行）  餘弦 + β·min(命中數, 3)，然後取 top-N
#   union（提議）        餘弦不動取 top-N，再把值命中的表聯集進去
#
# 為什麼 union 值得考慮：`format_catalog` 會把候選順序打散，所以 β 在候選
# 集合內部的排序資訊根本沒送到 LLM 面前 —— 它只剩「有沒有進候選」（二元，
# 聯集直接表達）與「誰是 ranked[0]」。而後者正是 ∪Top-1 那道保險的錨點，
# 它的設計理由是**誤差獨立**（餘弦 vs LLM 推理，機制不同），β 把它變成
# 混合訊號。實測（305 題、零 LLM）：
#
#     臂                 候選召回@40   Top-1 被值命中換掉   多帶候選
#     純餘弦（無值索引）      99.0%            0 題          0.00 張
#     beta β=0.05          100.0%            3 題          0.00 張
#     union                100.0%            0 題          0.01 張
#
# 硬指標打平，union 少污染 3 題錨點、少一個綁在今天嵌入模型餘弦分佈
# （Top-1 落在 0.29~0.42）上的尺度常數 —— 換嵌入模型時它會無聲過期。
# 2026-09-09 翻預設為 union。事前登記的三條判準在 305 題上全達標，
# 而且用 production 的程式路徑（不是探針的複製品）複驗過：
#     候選召回@40  100.0%（不低於 beta）｜多帶候選 0.01 張｜兩組保留組完全相同
# ⚠️ e2e 沒量 —— 兩臂在 13 題的候選集合與 3 題的錨點上不同。
#    這個改動的目的是**拆掉一個會無聲過期的常數**，不是換分數；
#    讓它跟著下一輪排定的六輪一起量，不要為它單獨觸發一輪。
#    一位元可逆：VALUE_MODE=beta。
VALUE_MODE = os.environ.get("VALUE_MODE", "union").lower()
if VALUE_MODE not in ("beta", "union"):
    raise ValueError(f"VALUE_MODE 只能是 beta 或 union，收到 {VALUE_MODE!r}")

VALUE_BETA = float(os.environ.get("VALUE_BETA", "0.05"))

# 候選目錄要不要附「這個值住在哪」。**2026-09-04 起預設 1 = 開啟**。
VALUE_EVIDENCE = int(os.environ.get("VALUE_EVIDENCE", "1"))

# 一個值最多跨幾張表才算有鑑別力。
#
# 「新北市」跨 6 張表（addresses/customers/employee_profiles/stores/
# suppliers/warehouses），報出來只是噪音 —— 那是鑑別力問題，不是門檻問題
# （同 [[discriminative-not-just-nonempty]]）。實測：≤2 時 13 題有證據、
# 不限時 22 題但多出來的九題全是跨表通用詞。
#
# 2 → 3：當初在「2 與不限」之間二選一，沒有量中間值。3 的實測是
#   撈到 GT 表的題 12 → 16（`#24`/`#55`/`#95`/`#98`，「3C數位」「家電」
#   這類值住在 categories＋products＋customer_profiles 三張表），**零題失去**；
#   代價是證據總字元 373 → 698（每題平均 1.2 → 2.3 字元，候選目錄本身約 2,880）。
# 這一格量的是值索引這一層；e2e 跟著下一輪多輪跑順帶收（同 VALUE_MODE=union）。
VALUE_MAX_TABLES = int(os.environ.get("VALUE_MAX_TABLES", "3"))

# 值至少要幾個字。長度 1 的值（性別 'M'、等級 '一'）會在任何句子裡誤中。
_MIN_LEN = 2

_index: dict[str, frozenset[tuple[str, str]]] | None = None
_lock = threading.Lock()


def _keep(value: str, schema_text: str) -> bool:
    """這個值像不像「會被問句點名的實體名」。

    兩道過濾，都不引進可調常數：

    ① **出現在任何一則 schema 註解裡的值是概念詞，不收。**
       `faq_articles` 有個分類值就叫「訂單」，而 305 題裡有 71 題的問句
       含這兩個字 —— 原型 15.4% 的精準度幾乎全毀在這裡。
       判準用既有的權威來源：「訂單」寫在 `orders` 的表註解裡，
       「iPhone 15」不在任何註解裡。**概念詞本來就是 dense 那一層的工作。**

       ⚠️ 這條有一個反作用要記著：表註解裡舉的例子會把該值踢出值索引。
       所以表註解不該再舉具體實體名當例子 —— 那件事現在由這一支負責。

    ② **純 ASCII 且短於 4 個字元不收**（'00'、'en'、'PE'、'AIR'）。
       中文不受這條限制：兩個漢字已經有足夠的資訊量。

    實測（全庫 305 題）：1,858 → 1,551 個值，精準度 15.4% → 32.8%，
    而候選層與 D 組漏出的結果**一格都沒變差** —— 砍掉的本來就沒在做事。
    """
    if len(value) < _MIN_LEN:
        return False
    if value.isascii() and len(value) < 4:
        return False
    return value.lower() not in schema_text


def get_value_index() -> dict[str, frozenset[tuple[str, str]]]:
    """`{值: {(表, 欄位), …}}`。建一次記在行程裡；失敗回空 dict 不拋。

    欄位一定要留著 —— 原型算出 `(t, c)` 之後只 `add(t)`，把欄位丟了。
    實測值得撿回來：**表對的時候欄位 100% 也對**（25/25），因為欄位不是猜的，
    是那個值真的存放的地方。
    """
    global _index
    if _index is not None:
        return _index
    with _lock:
        if _index is not None:
            return _index
        try:
            _index = _build()
        except Exception as e:
            log.warning(f"[ValueIndex] 建索引失敗（{type(e).__name__}: {e}），這條通道停用")
            _index = {}
        return _index


def _build() -> dict[str, frozenset[tuple[str, str]]]:
    acc: dict[str, set[tuple[str, str]]] = {}
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        schema_text = " ".join(
            [r[0] or "" for r in conn.execute(text(
                "SELECT TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE()"))]
            + [r[0] or "" for r in conn.execute(text(
                "SELECT COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE()"))]
        ).lower()
        cols = conn.execute(text(
            "SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME) "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND DATA_TYPE IN ('varchar', 'char', 'enum') "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION")).fetchall()
        for t, c in cols:
            for (v,) in conn.execute(text(
                    f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL")):
                v = str(v).strip()
                if _keep(v, schema_text):
                    acc.setdefault(v, set()).add((t, c))
    idx = {v: frozenset(tc) for v, tc in acc.items()}
    log.info(f"[ValueIndex] {len(idx)} 個值、"
             f"{len({tc for s in idx.values() for tc in s})} 個(表,欄位)組合、"
             f"掃過 {len(cols)} 個文字欄位")
    return idx


def matches(question: str) -> list[tuple[str, frozenset[tuple[str, str]]]]:
    """這一題命中了哪些值，各住在哪些 (表, 欄位)。

    兩道過濾：

    · **跨表太多的值不算**（`VALUE_MAX_TABLES`）—— 沒有鑑別力。
    · **最長匹配優先**：被更長的命中值包住的短值丟掉。
      「2026Q2 SOCIAL 投放」命中時就不要再報「SOCIAL」，
      「iPhone 15」命中時就不要再報 `search_logs` 裡的「iphone」。

      **包含比對要忽略大小寫**：`search_logs.keyword` 存的是「iphone」、
      `products.name` 存的是「iPhone 15」，用大小寫敏感的比對會判定
      「iphone 不是 iPhone 15 的子字串」而兩個都報出去 —— 那不是攤開歧義，
      是同一件事報兩次。**真正的歧義是「同一個值住在兩張表」**
      （「書籍」在 `categories.name` 也在 `products.category`），那種要留。

    比對方向是「值是問句的子字串」，不是反過來 —— 所以縮寫與換句話說
    （「蘋果的平板」對 `iPad Pro`）命中不了。**這是這條通道的天花板**，
    CHESS 用 LSH 容忍英文拼字錯誤，這裡刻意不做：中文沒有拼字錯誤，
    而換句話說是語意問題，本來就該由 dense 那一層負責。
    """
    idx = get_value_index()
    if not idx:
        return []
    q = question.lower()
    # 上限數的是**表**，不是 (表, 欄位) 對 —— 名字與上面的理由都是這樣寫的，
    # 但原本寫成 `len(tc)`。差別出在一張表有兩個同型欄位的情況：
    # `order_status_history` 有 `from_status` 與 `to_status`，於是每個訂單狀態值
    # 光在這張表就佔 2 格，加 `orders.status` 就是 3 > 2 而整個被丟掉。
    # 同一張表的兩個欄位不構成跨表歧義 —— 那正是 `get_value_index` 要留住欄位的理由。
    # 現況下這個修正是 0 行為變動（實測四種組合命中集合完全相同）；它要等代碼
    # 離開註解、那些值真的進得了索引之後才生效。
    hit = [(v, tc) for v, tc in idx.items()
           if v.lower() in q and len({t for t, _ in tc}) <= VALUE_MAX_TABLES]
    return [(v, tc) for v, tc in hit
            if not any(v != other and v.lower() in other.lower()
                       for other, _ in hit)]


def value_hits(question: str) -> dict[str, int]:
    """`{表: 這一題在它身上命中幾個值}` —— 給 dense 層加權用。"""
    out: dict[str, int] = {}
    for _v, tc in matches(question):
        for t, _c in tc:
            out[t] = out.get(t, 0) + 1
    return out


def evidence_for(question: str, tables: list[str]) -> dict[str, str]:
    """`{表: 要接在候選目錄那一行後面的字串}`。失敗回空 dict。

    只**報事實**：「這個值出現在本表的哪個欄位」。不寫「所以應該用這張表」——
    同一個值常常同時住在兩張表裡（「書籍」在 `categories.name` 也在
    `products.category`；「運動鞋」在 `products.name` 也在 `search_logs.keyword`），
    兩邊都是真的，該選哪張是 LLM 的工作，不是這一行的工作。

    ⚠️ 只有落在候選清單裡的表看得到自己的證據。如果某個值的另一個歸屬
    在候選層就被砍掉了，LLM 只會看到單邊 —— 那時這一行事實上變成了指向，
    是這個設計已知的邊界。
    """
    if not VALUE_EVIDENCE:
        return {}
    try:
        ms = matches(question)
    except Exception as e:
        log.warning(f"[ValueIndex] 取證據失敗（{type(e).__name__}），這一題不附證據")
        return {}
    if not ms:
        return {}
    want = set(tables)
    by_table: dict[str, list[str]] = {}
    for v, tc in ms:
        for t, c in sorted(tc):
            if t in want:
                by_table.setdefault(t, []).append(f"「{v}」= {c}")
    return {t: "｜值命中：" + "、".join(dict.fromkeys(items))
            for t, items in by_table.items()}
