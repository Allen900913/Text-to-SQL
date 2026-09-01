# -*- coding: utf-8 -*-
"""閘門第 [11] 項：橫向謂詞衝突 —— 同一件事被兩張**平行**的表各自記了一遍。

**為什麼閘門 [9] 結構上看不到（2026-09-01，ARCHITECTURE §7.11）**

閘門 [9]（`check_derived_consistency.py`）查的是：

    寬表的衍生欄位  vs  從**母表**現算        ← 縱向，前提是「存在可現算的母表」

`#258` 的病不是這個形狀。模型的 SQL 輸出欄位與 GT 逐字相同，只有 WHERE 不一樣：

    GT     WHERE product_profiles.lifecycle_stage = 'EOL'      12 筆
    模型   WHERE product_specs.is_discontinued    = 1           1 筆   交集 0

`product_specs` 與 `product_profiles` 是**兄弟**（都掛在 products 底下），
誰也不衍生自誰 —— `is_discontinued` 是 `db/init_db_ext.py:648` 的
`1 if random.random() < 0.1 else 0`，一顆獨立的骰子。**沒有母表可以現算，
所以閘門 [9] 不管跑幾次都是綠的。**

更硬的證據：同一份 GT 裡

    #127「已經停產的商品有哪些？」   → product_specs.is_discontinued = 1     1 個商品
    #258「列出所有已停產商品的…」    → product_profiles.lifecycle_stage='EOL' 12 個商品

**同一個詞「已停產」，兩題的正解是兩個互斥的集合。** `#258` 上模型做的事，
正是照 `#127` 的正解做的 —— 它不是選錯表，是答對了另一題。

> 判準沿用 §7.9：**冗餘可以，矛盾不行。**
> 這一支只是把那條線從縱向補到橫向。

**判準是資料，不是字串（這一點踩過兩次坑，不要再試第三次）**

本檔的原型版只用字串比對，309 題裡標了 161 題（52%）—— 和
`check_question_ambiguity.py` docstring 記的「第一版用餘弦標出一堆雜訊」
是同一個病。所以字串層**只當候選產生器**，判決一律回到資料。

phi 是 2x2 列聯表的相關係數，母體取兩張表的共同母表，兩邊都用 EXISTS：

    互補   交集 = 0 且 A + B = 母體         → A = NOT B，**正常**
    包含   交集 = min(A, B)                 → 小的是大的子集，**正常**
    互斥   交集 = 0 且 A + B < 母體         → 這個詞指向兩個不相交的集合
    獨立   |phi| < 0.25                     → 兩顆互不相干的骰子
    一致   其餘                             → 冗餘，正是 93 張表的設計目的

⚠️ **「交集 0」本身不是缺陷。** 第一版少了「互補」那一條，於是
`employee_profiles.resigned_at` vs `employees.is_active`（phi = -1.000、
A 2 + B 22 = 母體 24）被判成紅燈 —— 那兩個謂詞是互補的，離職 = 非在職。

`#202` 實測 phi = +0.006（orders 200 張、pref='STORE' 58 張、
已取貨 44 張、交集 13，獨立亂數的期望交集 58*44/200 = 12.8）
—— 兩張記錄同一件事的表統計上完全獨立。

**能機械化的謂詞**（自動判決）與**不能的**（列出來給人判）分開報，
不假裝覆蓋率是 100%：

    布林旗標      TINYINT              -> col = 1
    可空時間戳    DATE/DATETIME + NULL -> col IS NOT NULL
    列舉值        VARCHAR 且註解有中文對照 -> col = '<代碼>'
                  例：'配送偏好：HOME 宅配／STORE 門市取貨／LOCKER 智取櫃'
                      詞組「取貨」-> delivery_preference = 'STORE'
    表存在性      有 FK 指向母表的事實表 -> EXISTS(該表有這個實體的列)

    無法機械化    純英文列舉（order_returns.status '(REQUESTED/RECEIVED/REJECTED)'）
                  沒有中文對照，詞組對不上代碼 -> 進人工判讀清單

**燈號**（判決靠 `ADJUDICATED`，見下方那張表為什麼必須存在）

    紅（exit 1）未裁決的衝突         —— 新出現的配對，逼人看一眼
    紅（exit 1）裁決為「待修」還沒修 —— 一直紅到資料或問句改好
    黃（exit 0）沒有 GT 引用的衝突   —— 地雷，下次配題問到就會變紅燈
    靜音        裁決為「不同事」或「宣告」
    綠          互補／包含／phi 高

**不擋閘門、只列出來的兩桶**（加 `--all` 才印）：一邊是「表裡有沒有列」的，
以及謂詞無法機械化的（純英文列舉 `'(REQUESTED/RECEIVED/REJECTED)'` 對不上中文詞組）。
兩者都不主張謂詞，自動判它們會製造大量誤報。

純 SQL + regex，零 LLM，**不寫任何東西進資料庫**（全部是 SELECT）。
用法：`.venv/Scripts/python.exe tools/check_predicate_collisions.py`
      加 `--all` 連一致的配對與兩桶待人工的一起印出來。
"""
import io
import math
import os
import re
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import json  # noqa: E402
import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.schema_graph import get_foreign_keys  # noqa: E402

# ---------------------------------------------------------------------------
# 裁決表 —— 為什麼要有這個，而不是把字串層調準
#
# 字串層永遠會把「兩件不同的事共用一個詞」配成一對：
#     subscription_profiles.opt_out_marketing 『是否退出行銷訊息』
#     subscriptions.ended_at                  『訂閱結束日』        共用詞組「訂閱」
# 兩個都是合法謂詞、都掛在同一個實體上、phi 也真的接近 0 —— 但它們本來就無關，
# phi=0 是它們**應該**有的樣子。要判斷「這兩個謂詞說的是不是同一件事」，
# 需要的是語意判斷，而**調字串門檻去逼近語意判斷正是本專案禁止的事**
# （`check_question_ambiguity.py` 的餘弦版、本檔的 52% 原型，已經失敗兩次）。
#
# 所以照 `check_question_ambiguity.py` 的 `EXPECTED_DIVERGENT` 那條路：
# **人工裁決一次，閘門之後只守迴歸。** 新出現的配對一律紅燈，逼人來看一眼。
#
#     不同事  兩個謂詞說的不是同一件事 -> 靜音
#     待修    同一件事但值對不上 -> 一直紅燈，直到資料或問句修好
#     宣告    同一件事、值本來就該不同（偏好 vs 實際）-> 靜音，但理由要寫下來
# ---------------------------------------------------------------------------
def _k(t1, c1, t2, c2):
    return tuple(sorted([(t1, c1), (t2, c2)]))


ADJUDICATED: dict[tuple, tuple[str, str]] = {
    _k("product_profiles", "lifecycle_stage", "product_specs", "is_discontinued"):
        ("待修", "同一件事（這個商品停產了沒），12 筆 vs 1 筆、交集 0。"
                 "真相來源＝product_profiles（lifecycle_stage 與 delisted_at 兩欄互相佐證，各 12 筆）；"
                 "is_discontinued 是 db/init_db_ext.py:648 的獨立骰子。#127 與 #258 現在答案互斥"),

    _k("order_profiles", "delivery_preference", "store_pickups", "picked_up_at"):
        ("宣告", "偏好 vs 實際 —— 這兩個量**本來就可以不同**（偏好門市但最後宅配），"
                 "對齊會毀掉語意（§7.9 對 #279 的處置同型），所以不動資料。"
                 "2026-09-01 已改問句：#202 原本的「選了到店取貨」逐字指向 delivery_preference，"
                 "改成 #221 已在用的「有到店取貨紀錄的訂單裡…」。"
                 "⚠️ 未解：phi=+0.006 是**完全獨立**，真實系統會強正相關 —— "
                 "那是「資料不夠像真的」，不是「題目沒有唯一答案」，優先級低，先擱著"),

    _k("customer_profiles", "newsletter_opt_in", "newsletter_subscriptions", "unsubscribed_at"):
        ("宣告", "**閘門自己找到的，不是手動翻出來的。** 2026-09-02 已裁決並對齊資料："
                 "真相來源＝newsletter_subscriptions，opt_in 現在**定義為**"
                 "EXISTS(該客戶未退訂的列)，22 -> 18，24 位客戶的旗標重新指派。\n"
                 "     為什麼修完還是「交集 0、A+B < 母體」而**這是對的**："
                 "每位客戶只有一列電子報訂閱（22 列 / 22 人），所以「現在訂著」與"
                 "「已退訂」天生互斥；剩下的 28 位是從來沒訂過的人，兩邊都不算。"
                 "**互斥只有在兩個謂詞宣稱切開整個母體時才是缺陷** —— 這一組沒有，"
                 "就像 employee_profiles.resigned_at vs employees.is_active 的互補一樣。\n"
                 "     迴歸由誰守：這一欄已經進 tools/fix_derived_consistency.py，"
                 "它每次跑都會報「要改 0 列」，資料一漂就會變成非 0。"),

    _k("subscription_profiles", "opt_out_marketing", "subscriptions", "ended_at"):
        ("不同事", "退出行銷訊息 ≠ 訂閱結束，共用詞組「訂閱」而已"),
    _k("subscription_profiles", "converted_from_trial", "subscriptions", "ended_at"):
        ("不同事", "試用轉付費 ≠ 訂閱結束，共用詞組「結束」而已"),
    _k("newsletter_subscriptions", "unsubscribed_at", "subscriptions", "ended_at"):
        ("不同事", "電子報收信意願 ≠ 付費訂閱方案 —— newsletter_subscriptions 的表註解自己就寫了"),
    _k("customer_profiles", "newsletter_opt_in", "subscriptions", "ended_at"):
        ("不同事", "訂閱電子報 ≠ 付費訂閱結束"),
    _k("order_profiles", "contact_before_delivery", "shipments", "delivered_at"):
        ("不同事", "要求送達前先電聯 ≠ 已送達，共用詞組「送達」而已"),
}

# 詞組太泛會讓每一題都命中。這裡只擋「任何正規化 schema 都到處都是」的詞。
STOP = set("""時間 名稱 客戶 商品 訂單 資料 紀錄 數量 金額 總共 哪些 多少 什麼 幾個
幾筆 幾張 列出 統計 目前 現在 每個 所有 這些 那些 有沒有 分別 包含 以及 還有 我想
的商 商品 品的 訂單 單的""".split())

_PHI_INDEPENDENT = 0.25   # |phi| 低於此視為「兩顆獨立的骰子」


# ---------------------------------------------------------------------------
# 1. 從 semantic_layer.yaml 剖析 DDL
# ---------------------------------------------------------------------------
def parse_ddl() -> tuple[dict, dict]:
    """回傳 ({表: [(欄, 型別, 註解)]}, {表: 表註解})。"""
    raw = yaml.safe_load(io.open(
        os.path.join(_ROOT, "utils", "semantic_layer.yaml"), encoding="utf-8"))["ddl"]
    cols: dict[str, list] = {}
    tcomment: dict[str, str] = {}
    cur = None
    for line in raw.splitlines():
        m = re.search(r"CREATE TABLE\s+`?(\w+)`?", line)
        if m:
            cur = m.group(1)
            cols[cur] = []
            continue
        if cur is None:
            continue
        m = re.match(r"\s*\)\s*COMMENT\s+'(.*)'", line)
        if m:
            tcomment[cur] = m.group(1)
            continue
        m = re.match(r"\s+(\w+)\s+([A-Za-z]+(?:\([\d,]+\))?)\s+.*?COMMENT\s+'([^']*)'", line)
        if m and m.group(1).upper() not in ("PRIMARY", "FOREIGN", "KEY", "UNIQUE"):
            cols[cur].append((m.group(1), m.group(2).upper(), m.group(3)))
    return cols, tcomment


# ---------------------------------------------------------------------------
# 2. 謂詞：把「一個欄位 + 一個中文詞組」變成一段可執行的 WHERE
# ---------------------------------------------------------------------------
_ENUM = re.compile(r"([A-Z][A-Z0-9_]{1,})\s+([一-鿿]{2,10})")


def enum_options(comment: str) -> dict[str, str]:
    """'配送偏好：HOME 宅配／STORE 門市取貨' -> {'HOME': '宅配', 'STORE': '門市取貨'}"""
    return {code: zh for code, zh in _ENUM.findall(comment)}


def is_predicate_col(typ: str, comment: str) -> bool:
    """這個欄位**看起來像不像**一個是非題？不像的話連候選都不該進。

    沒有這一關，`campaign_profiles.currency_code 『投放幣別』` 會因為
    詞組「投放」和 `campaigns.channel 『投放渠道』` 配成一對 —— 兩個都不是
    謂詞，配起來沒有任何意義。第一版的 227 組人工清單大半是這種。
    """
    if typ == "TABLE":                       # 表存在性
        return True
    if typ.startswith("TINYINT"):
        return True
    if typ in ("DATE", "DATETIME") and any(w in comment for w in ("NULL", "未", "為空")):
        return True
    if typ.startswith("VARCHAR"):
        # 有中文對照的列舉，或圓括號英文列舉 '(REQUESTED/RECEIVED/REJECTED)'
        return bool(enum_options(comment)) or bool(re.search(r"\([A-Z_]+(/[A-Z_]+)+\)", comment))
    return False


def predicate_for(col: str, typ: str, comment: str, phrase: str) -> str | None:
    """這個欄位能不能機械化成一段可執行的 WHERE？不能就回 None。"""
    if typ == "TABLE":
        return "1 = 1"                       # 表存在性：有這個實體的列就算 true
    if typ.startswith("TINYINT"):
        return f"{col} = 1"
    if typ in ("DATE", "DATETIME") and any(w in comment for w in ("NULL", "未", "為空")):
        return f"{col} IS NOT NULL"
    if typ.startswith("VARCHAR"):
        for code, zh in enum_options(comment).items():
            if phrase in zh:
                return f"{col} = '{code}'"
    return None                               # 純英文列舉對不上中文詞組 -> 人工判讀


# ---------------------------------------------------------------------------
# 3. 兩張表怎麼比：找共同母表，母表的每一列當一個樣本
# ---------------------------------------------------------------------------
def build_parent_map(fks) -> dict[str, list[tuple[str, str, str]]]:
    """{子表: [(子表的欄, 母表, 母表的欄)]}"""
    out = defaultdict(list)
    for fk in fks:
        out[fk.table].append((fk.column, fk.ref_table, fk.ref_column))
    return out


def common_parent(a: str, b: str, pmap) -> tuple[str, str, str, str, str] | None:
    """回傳 (母表, 母表PK, A的FK欄, B的FK欄, 說明)。找不到就 None。"""
    for ca, pa, pca in pmap.get(a, []):
        for cb, pb, pcb in pmap.get(b, []):
            if pa == pb and pca == pcb:
                return pa, pca, ca, cb, f"共同母表 {pa}"
    # A 直接指向 B（B 就是母表）
    for ca, pa, pca in pmap.get(a, []):
        if pa == b:
            return b, pca, ca, pca, f"{a} 直接指向 {b}"
    for cb, pb, pcb in pmap.get(b, []):
        if pb == a:
            return a, pcb, pcb, cb, f"{b} 直接指向 {a}"
    return None


def phi_of(conn, parent, ppk, ta, ca_fk, pred_a, tb, cb_fk, pred_b):
    """母表每一列當一個樣本，兩邊都用 EXISTS。回傳 (n, a, b, ab, phi)。"""
    # 謂詞不能無腦加表名前綴 —— 表存在性的謂詞是 "1 = 1"，
    # 加了會變成 `addresses.1 = 1`（第一版 227 組人工清單裡的 OperationalError 全是這個）。
    qa = pred_a if pred_a == "1 = 1" else f"{ta}.{pred_a}"
    qb = pred_b if pred_b == "1 = 1" else f"{tb}.{pred_b}"
    ea = f"EXISTS(SELECT 1 FROM {ta} WHERE {ta}.{ca_fk} = P.{ppk} AND {qa})"
    eb = f"EXISTS(SELECT 1 FROM {tb} WHERE {tb}.{cb_fk} = P.{ppk} AND {qb})"
    row = conn.execute(text(
        f"SELECT COUNT(*), SUM({ea}), SUM({eb}), SUM({ea} AND {eb}) FROM {parent} P"
    )).fetchone()
    n, a, b, ab = (int(x or 0) for x in row)
    c11, c10, c01 = ab, a - ab, b - ab
    c00 = n - a - b + ab
    den = math.sqrt((c11 + c10) * (c01 + c00) * (c11 + c01) * (c10 + c00))
    phi = (c11 * c00 - c10 * c01) / den if den else float("nan")
    return n, a, b, ab, phi


# ---------------------------------------------------------------------------
# 4. 候選產生：問句驅動
# ---------------------------------------------------------------------------
_CJK = re.compile(r"[一-鿿]{2,}")
_TAB = re.compile(r"(?:FROM|JOIN)\s+`?([A-Za-z_]\w*)`?", re.I)


def phrases(question: str) -> set[str]:
    out = set()
    for seg in _CJK.findall(question):
        for n in (2, 3):
            for i in range(len(seg) - n + 1):
                w = seg[i:i + n]
                if w not in STOP:
                    out.add(w)
    return out


def main() -> int:
    log.remove()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    cols, tcomment = parse_ddl()
    gt = {x["id"]: x for x in yaml.safe_load(
        io.open(os.path.join(_ROOT, "eval_ground_truth.yaml"), encoding="utf-8"))}
    qs = {x["id"]: x for x in json.load(
        io.open(os.path.join(_ROOT, "eval_questions_v2.json"), encoding="utf-8"))}

    # 詞組 -> 擁有它的 (表, 欄, 型別, 註解)；表註解算在欄位 None 上（表存在性謂詞）
    owners: dict[str, list] = defaultdict(list)
    for t, cs in cols.items():
        for c, typ, cm in cs:
            for n in (2, 3):
                for i in range(len(cm) - n + 1):
                    owners[cm[i:i + n]].append((t, c, typ, cm))
        cm = tcomment.get(t, "")
        for n in (2, 3):
            for i in range(len(cm) - n + 1):
                owners[cm[i:i + n]].append((t, None, "TABLE", cm))

    pmap = build_parent_map(get_foreign_keys())

    # GT 引用了哪些表
    gt_tables: dict[str, set] = defaultdict(set)
    for qid, g in gt.items():
        for t in _TAB.findall(g.get("sql") or ""):
            gt_tables[t.lower()].add(qid)

    # (表A,欄A,表B,欄B) -> {詞組, 觸發的題}
    cand: dict[tuple, dict] = {}
    for qid, g in gt.items():
        gtabs = {t.lower() for t in _TAB.findall(g.get("sql") or "")}
        if not gtabs:
            continue
        for w in phrases(qs.get(qid, {}).get("question", "")):
            own = [o for o in owners.get(w, []) if is_predicate_col(o[2], o[3])]
            # 「太泛的詞」要在**篩掉非謂詞之後**才數，否則
            # 「取貨」會因為 store_profiles.offers_pickup 與 service_appointments
            # 的表註解湊到 4 張表而被整個丟掉 —— #202 就是這樣漏掉的。
            tabs = {o[0] for o in own}
            if not tabs or len(tabs) > 4:
                continue
            inside = [o for o in own if o[0] in gtabs]
            outside = [o for o in own if o[0] not in gtabs]
            if not inside or not outside:
                continue
            for ia in inside:
                for ob in outside:
                    key = tuple(sorted([(ia[0], ia[1]), (ob[0], ob[1])]))
                    e = cand.setdefault(key, {"w": set(), "q": set(), "meta": {}})
                    e["w"].add(w)
                    e["q"].add(qid)
                    e["meta"][(ia[0], ia[1])] = ia
                    e["meta"][(ob[0], ob[1])] = ob

    print(f"候選配對 {len(cand)} 組（問句驅動，字串層）\n")

    red, yellow, green, declared, manual, review, todo, fixed = [], [], [], [], [], [], [], []
    db = get_db_manager(MYSQL_URI)
    with db.engine.connect() as conn:
        for key, e in sorted(cand.items(), key=lambda kv: [(t, c or '') for t, c in kv[0]]):
            (ta, ca), (tb, cb) = key
            if ta == tb:
                continue
            ia, ib = e["meta"][(ta, ca)], e["meta"][(tb, cb)]
            words = sorted(e["w"], key=len, reverse=True)

            link = common_parent(ta, tb, pmap)
            if link is None:
                continue                        # 接不起來就沒得比

            # 兩邊各自能不能機械化（詞組逐個試，長的先）
            pa = pb = None
            for w in words:
                pa = pa or predicate_for(ca, ia[2], ia[3], w)
                pb = pb or predicate_for(cb, ib[2], ib[3], w)
            if pa is None or pb is None:
                manual.append((ta, ca, ia[3], tb, cb, ib[3], words[:3], sorted(e["q"])))
                continue

            parent, ppk, fa, fb, how = link
            try:
                n, na, nb, nab, phi = phi_of(conn, parent, ppk, ta, fa, pa, tb, fb, pb)
            except Exception as ex:
                manual.append((ta, ca, ia[3], tb, cb, ib[3],
                               [f"SQL失敗 {type(ex).__name__}"], sorted(e["q"])))
                continue

            if min(na, nb) == 0 or na == n or nb == n:
                continue                        # 退化，比不出東西
            qids = sorted(e["q"])
            cited = sorted({q for t in (ta, tb) for q in gt_tables.get(t, set())})
            rec = (phi, n, na, nb, nab, ta, ca, pa, tb, cb, pb, words[:3], qids, cited, how)

            # 判決。**「交集 0」本身不是缺陷** —— 第一版把它一律當矛盾，
            # 於是 employee_profiles.resigned_at vs employees.is_active
            # （phi = -1.000、A 2 + B 22 = 母體 24）被誤判成紅燈。
            # 那兩個謂詞是**互補**的：離職 = 非在職，交集 0 正是它該有的樣子。
            # 兩層信心，而且是**結構上**分的，不是調門檻分的：
            #   欄位謂詞  註解寫著「是否已停產」的欄位，**主張**自己編碼那個事實 -> 自動判決
            #   表存在性  「這個實體在這張表有列」**沒有主張**任何事實 -> 只報告，不判決
            # 沒有這一層，product_qna 與 support_tickets（都掛在 customers 底下、
            # 彼此本來就無關）會因為 phi≈0 被判成矛盾。它們不是矛盾，是兩件不同的事。
            existence = (ca is None or cb is None)
            consistent = (nab == 0 and na + nb == n) or nab == min(na, nb) \
                or abs(phi) >= _PHI_INDEPENDENT
            verdict = ADJUDICATED.get(tuple(sorted([(ta, ca), (tb, cb)])))
            if verdict:
                # 裁決為「待修」的**要回頭看資料**：修好了就自己放行。
                # 沒有這一步，修完之後閘門會永遠紅燈，於是三天後被人忽略。
                if verdict[0] != "待修":
                    declared.append((verdict, rec))
                elif consistent:
                    fixed.append((verdict, rec))
                else:
                    todo.append((verdict, rec))
            elif nab == 0 and na + nb == n:
                green.append(rec)                             # 互補：A = NOT B
            elif nab == min(na, nb):
                green.append(rec)                             # 包含：小的是大的子集
            elif nab == 0:
                (review if existence else (red if cited else yellow)).append(("互斥", rec))
            elif abs(phi) < _PHI_INDEPENDENT:
                (review if existence else (red if cited else yellow)).append(("獨立", rec))
            else:
                green.append(rec)

    # ---------------- 報表 ----------------
    def show(tag, rec):
        (phi, n, na, nb, nab, ta, ca, pa, tb, cb, pb, words, qids, cited, how) = rec
        print(f"  [{tag}] phi={phi:+.3f}  母體 {n}｜A {na}｜B {nb}｜交集 {nab}   （{how}）")
        print(f"         A  {ta}.{pa}")
        print(f"         B  {tb}.{pb}")
        print(f"         詞組 {words}｜觸發題 {qids[:8]}｜GT 引用 {cited[:8]}")

    print("=" * 92)
    print(f"未裁決 {len(red)}｜黃燈 {len(yellow)}｜待修 {len(todo)}｜已修 {len(fixed)}｜"
          f"已裁決靜音 {len(declared)}｜一致 {len(green)}｜"
          f"待人工：表存在性 {len(review)}、無法機械化 {len(manual)}")
    print("=" * 92)

    if fixed:
        print("\n### 已修：裁決過，資料現在對得上了\n")
        for (v, why), rec in sorted(fixed, key=lambda x: x[1][0]):
            show("已修", rec)
            print(f"         裁決  {why.splitlines()[0]}")
            print()
    if todo:
        print("\n### 待修：同一件事，值對不上\n")
        for (v, why), rec in sorted(todo, key=lambda x: x[1][0]):
            show(v, rec)
            print(f"         裁決  {why}")
            print()
    if red:
        print("\n### 未裁決：新出現的配對，看一眼並寫進 ADJUDICATED\n")
        for tag, rec in sorted(red, key=lambda x: x[1][0]):
            show(tag, rec)
            print()
    if yellow:
        print("\n### 黃燈：沒有 GT 引用（地雷，下次配題問到就變紅）\n")
        for tag, rec in sorted(yellow, key=lambda x: x[1][0]):
            show(tag, rec)
            print()
    if review and "--all" in sys.argv:
        print(f"\n### 待人工：一邊是「表裡有沒有列」，那不主張任何事實（{len(review)} 組）\n")
        for tag, rec in sorted(review, key=lambda x: abs(x[1][0])):
            show(tag, rec)
            print()
    if manual and "--all" in sys.argv:
        print(f"\n### 待人工：謂詞無法機械化（{len(manual)} 組）\n")
        for ta, ca, cma, tb, cb, cmb, words, qids in manual[:40]:
            print(f"  {ta}.{ca or '(表)'} 『{cma[:34]}』")
            print(f"  {tb}.{cb or '(表)'} 『{cmb[:34]}』  詞組 {words}｜題 {qids[:6]}")
            print()
    if green and "--all" in sys.argv:
        print(f"\n### 一致（冗餘，這是設計目的）{len(green)} 組\n")
        for rec in sorted(green, key=lambda x: -x[0]):
            show("一致", rec)
            print()

    if review or manual:
        print(f"\n（待人工的 {len(review)} + {len(manual)} 組沒有印出來，加 --all 看全部。"
              "它們不擋閘門 —— 一邊是表存在性、一邊是純英文列舉，兩種都不主張謂詞。）")

    bad = []
    if red:
        bad.append(f"未裁決的謂詞衝突 {len(red)} 組 —— 逐一判定後寫進 ADJUDICATED")
    if todo:
        bad.append(f"已裁決為「待修」但還沒修 {len(todo)} 組")
    if bad:
        print("\n閘門 [11] 紅燈：")
        for b in bad:
            print("  - " + b)
        print("\n處置依 §7.9：路徑唯一就改資料（指定真相來源後對齊），")
        print("             路徑不唯一就改問句（兩個量本來就不同，對齊會毀掉語意）。")
        return 1
    print("\n閘門 [11] 綠燈：沒有未裁決、也沒有待修的橫向謂詞衝突。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
