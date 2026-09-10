# -*- coding: utf-8 -*-
"""表註解的分句型宣告 —— 一個來源，三個投影。**不 import 任何其他 utils。**

為什麼需要這個模組
====================================================================
表註解一段文字身兼三職（DDL 給生成器 / 向量給餘弦 / 目錄給選表 LLM），
而它裡面混了六種句型。三個消費端的**能力不同**，所以最佳輸入不同：

    句型              檢索向量（餘弦）   候選目錄（LLM）   DDL（生成器）
    content 粒度內容        要              要              要
    enum    值域          已量：加了更差      —          走 get_enum_text()
    usage   用途          要（實測承重）       要              要
    facet   我是什麼         要              要              要
    bound   我不是什麼        要              要              要
    ptr     X 在 表A     **有害（8/8）**      要              要

`ptr` 那一格是硬證據：`_strip_relations` 修好的 8 題，每一題的正解表都是
指路標點名的那張表。**嵌入不懂否定** —— 寫「星等與評價文字在 reviews」，
餘弦只看到文件裡有 `reviews`，於是 review_profiles 在問 reviews 的題上排第一。
LLM 讀得懂那句話，餘弦讀不懂。

兩種分欄機制，只有一種安全
====================================================================
    加管道（enum 用的）   只能「多給某一端」   不碰原文，不會出錯   代價：原地留重複
    切字串（strip 用的）  能「少給某一端」    **會切錯，而且靜默**

`ptr` 需要的是「少給檢索」，只能用切。所以這裡不在讀取時用 regex 切散文，
而是**在來源就把句型分開**：切這件事只在遷移時做一次，並且由重組驗證。

無損的證明
====================================================================
每個子句連同它後面的分隔符一起存，所以 `compose()` 是逐字元的反函數：

    compose(decompose(c)) == c      對全部 93 張表

這就是遷移的驗收條件。`_strip_relations` 當年出錯，正是因為它丟掉子句之後
用「。」盲目重接 —— 分隔符沒有跟著子句走。

用法：
    from langgraph_sql.utils.table_semantics import brief_for, KINDS
    brief_for("orders", "retrieval")
"""
import os
import re
import threading

import yaml

# 六種句型。順序就是「從最該留到最可疑」，投影表照這個順序讀比較好核對。
KINDS = ("content", "enum", "usage", "facet", "bound", "ptr")

# 三個消費端各自收哪些句型。**預設值刻意重現今天的行為**（三個投影都是全收），
# 這樣重構本身是位元中性的，行為改變才能各自帶自己的臂與判準（§「A/B 的臂
# 釘在版本不是旗標」）。要翻 ptr 那一格，改這張表並且帶一個對照組。
#
# 2026-09-10：`enum` 那一格翻了。理由不是分數，是**通道衝突** ——
# `value_index._keep()` 丟掉任何出現在 schema 註解裡的值，所以只要表註解
# 還寫著 PENDING/PAID/…，值索引就永遠收不到那些代碼（實測 14 題含代碼的
# 問句，GT 命中 0）。代碼的家在 `enum_fields`（走 `enum_text()` 直送生成器），
# 不在檢索文件裡。三個角色分開處理：
#   ddl        拿掉 —— 它就是資料庫的 TABLE_COMMENT，`_keep` 讀的正是這一份
#   retrieval  **留著** —— 見下
#   catalog    **留著** —— 選表 LLM 讀得懂代碼，而候選目錄不進 `_keep`，零代價
#
# retrieval 那一格試過拿掉，量下來要留：`_keep` 讀的是**資料庫**的 TABLE_COMMENT，
# 而檢索文件是另一份行程內字串，從來不進 `_keep` —— 也就是說拿掉它對解鎖值索引
# 毫無貢獻，卻讓 `orders` 少 48 字元，扣掉硬幣後淨 -3（`#9`/`#10`/`#50` 全是
# `orders` 輸給 `payments`／`order_cancellations`）。**能達成目的的最小改動優先**：
# 代碼離開資料庫註解就夠了，檢索文件那份留著。
PROJECTIONS = {
    "ddl":       tuple(k for k in KINDS if k != "enum"),
    "catalog":   KINDS,
    "retrieval": KINDS,
}

# 環境變數覆寫，格式 `TS_<ROLE>=kind,kind,...`，例如砍掉檢索文件的指路標：
#     TS_RETRIEVAL=content,enum,usage,facet,bound
# **臂要釘在版本不是旗標**：`effective()` 印得出行程內真正生效的投影，
# 跑 A/B 時 assert 它，不要只信環境變數（跑到一半改檔案，同一個旗標
# 前後會指向不同模式）。
for _role in tuple(PROJECTIONS):
    _env = os.environ.get("TS_" + _role.upper())
    if _env:
        _ks = tuple(k.strip() for k in _env.split(",") if k.strip())
        bad = [k for k in _ks if k not in KINDS]
        if bad:
            raise ValueError(f"TS_{_role.upper()} 有未知句型 {bad}；合法值 {KINDS}")
        PROJECTIONS[_role] = _ks


def effective() -> dict[str, tuple[str, ...]]:
    """行程內真正生效的投影表。A/B 時 assert 這個，不要 assert 環境變數。"""
    return dict(PROJECTIONS)

_PATH = os.path.join(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))), "utils", "table_semantics.yaml")

_data = None
_enum_order: list = []
_lock = threading.Lock()

# 投影時可能留在尾巴的懸空分隔符。原文有的表以「。」收尾、有的沒有，
# 所以只清掉懸空的連接號，不強迫補句號 —— 補了就不是位元中性。
_DANGLING = "，、；;——  　"


def _read(path: str | None = None) -> dict:
    """{表名: {"clauses": [...], "columns": {欄位: 註解}}}。一張表的全部資訊在一起。

    欄位註解**不分句型**，就是純字串。理由是實測沒有分的依據：唯一有機會的
    假設是「`total_spent` 的快照警語讓欄位提示在陷阱題上贏過正解欄位」，
    量下來被推翻 —— 砍掉警語確實壓低陷阱題（`#94` −0.035、`#100` −0.043），
    但把**正解題壓得更低**（`#146` −0.049、`#147` −0.109，後者正是靠
    「與即時 SUM(orders.total_amount) 可能不同」命中的）。又是同一條雙向軸。
    **沒有證據就不建機制**；要建的時候，clauses 那套現成的可以照套。
    """
    global _data
    if _data is not None and path is None:
        return _data
    with _lock:
        with open(path or _PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        global _enum_order
        _enum_order = list(raw.get("enum_order") or [])
        out = {}
        for t, spec in (raw.get("tables") or {}).items():
            clauses = list(spec.get("clauses") or [])
            for c in clauses:
                if c["kind"] not in KINDS:
                    raise ValueError(f"{t}: 未知句型 {c['kind']}")
            out[t] = {"clauses": clauses,
                      "columns": dict(spec.get("columns") or {}),
                      "enums": dict(spec.get("enums") or {})}
        if path is None:
            _data = out
        return out


def load(path: str | None = None) -> dict:
    """{表名: [{kind, text, sep}, ...]}。只要子句時用這個。"""
    return {t: s["clauses"] for t, s in _read(path).items()}


def columns() -> dict[tuple[str, str], str]:
    """{(表, 欄位): 欄位註解}。sync_table_comments 的欄位那一半吃這個。"""
    return {(t, c): cm for t, s in _read().items()
            for c, cm in s["columns"].items()}


def enums() -> dict[str, dict]:
    """{"表.欄位": {description, values}}。值域是**宣告**的，不從註解措辭推導。

    舊做法讓值域變成散文的函數：`gen_enum_fields.py` 判斷「這段註解有沒有把
    值域講清楚」，講不清楚的才收。用意（別把 DDL 已有的再送一次）對，
    但耦合已經斷了 —— 分級守衛 `if len(hit) < 2: continue` 要求註解**已經**
    提到至少 2 個值才往下走，於是「一個值都沒提」（正是 R2 的定義）的
    17 個代碼欄位連分級都沒進到；而線上 52 欄用產生器重跑只剩 0 欄。

    去重改用投影表達，不用分類器：值域住在這裡，誰要誰拿。
    """
    d = _read()
    out = {f"{t}.{c}": v for t, s in d.items() for c, v in s["enums"].items()}
    # 值域**按表分組**存放（一張表的東西在一起，才好維護），但 Prompt 的
    # 列出順序是另一回事：手寫時代是「核心表在前」（orders / payments /
    # shipments 打頭），跟 gen_ddl 的 TABLE_ORDER 同一個理由 ——
    # 模型從上往下讀。分組會把那個順序洗掉，所以順序單獨記在 enum_order，
    # 讓儲存結構與呈現順序各自獨立。少了它，多表 scoped 呼叫就不是位元中性的。
    if _enum_order:
        rank = {k: i for i, k in enumerate(_enum_order)}
        out = dict(sorted(out.items(), key=lambda kv: (rank.get(kv[0], 10**6), kv[0])))
    return out


# 補漏的值域（`arm: gap`）預設**宣告但不送**。分開這兩件事的理由：
#   宣告要完整 —— 閘門 [4b] 才驗得到「宣告的值資料裡還在不在」，
#                 而漏宣告的欄位跟漏登記的來源一樣，不可能被檢查到。
#   送不送是行為 —— 它會改生成器的 Prompt（+1530 字元，scoped），
#                 尤其 customer_profiles 現在 enum_text 是 0 字元而 18 題 GT
#                 需要它，其中包含陷阱家族 `#146`/`#147`。那是要帶判準的臂。
# 一位元可逆：TS_ENUM_GAPS=1。
ENUM_GAPS = os.environ.get("TS_ENUM_GAPS", "0") == "1"


def enum_text(tables=None) -> str:
    """給生成器看的值域說明。與 schema_parser.get_enum_text() 逐字相同 ——
    重構要位元中性，行為改變另外開臂。"""
    wanted = None if tables is None else {t.lower() for t in tables}
    lines: list[str] = []
    for field, info in enums().items():
        if wanted is not None and field.split(".")[0] not in wanted:
            continue
        if info.get("arm") == "dup":
            continue          # 值已在欄位註解裡，DDL 就帶著了，再送是重複
        if info.get("arm") == "gap" and not ENUM_GAPS:
            continue
        lines.append(f"  {field} ({info.get('description', '')})")
        for val, desc in (info.get("values") or {}).items():
            lines.append(f"    - '{val}' = {desc}" if desc else f"    - '{val}'")
    return "\n".join(lines)


def compose(clauses: list[dict], kinds: tuple[str, ...] = KINDS) -> str:
    """把子句接回一段註解。kinds 給全集時是 decompose 的逐字元反函數。

    投影掉某個句型會讓相鄰子句的前導空白露出來（`orders` 拿掉 enum 之後是
    「總金額。 查待處理…」）。收掉重複空白**只在真的投影掉東西時**做，
    全集時仍是逐位元反函數 —— 往返無損那條驗收不能因為美化而失效。
    """
    text = "".join(c["text"] + c["sep"] for c in clauses if c["kind"] in kinds)
    if len(kinds) < len(KINDS):
        text = re.sub(r" {2,}", " ", text).replace("。 ", "。")
    return text.rstrip(_DANGLING)


def brief_for(table: str, role: str, data: dict | None = None) -> str:
    """某張表給某個消費端看的註解。role ∈ PROJECTIONS。"""
    d = data if data is not None else load()
    clauses = d.get(table.lower())
    if clauses is None:
        return ""
    return compose(clauses, PROJECTIONS[role])


def briefs_for(role: str, data: dict | None = None) -> dict[str, str]:
    d = data if data is not None else load()
    return {t: compose(cl, PROJECTIONS[role]) for t, cl in d.items()}
