# -*- coding: utf-8 -*-
"""把 93 張表的註解拆成分句型宣告 —— 一次性遷移，由重組驗證無損

為什麼這樣做
====================================================================
表註解現在散在七個來源、三種格式（SQL 文字 34 張 / Python tuple 63 張 /
YAML 宣告 13 張），要三支解析器再加一份**手寫**的來源登記表串起來 ——
而那份登記表已經漏了一份（`wide_table_plan_2.yaml` 的 7 張表沒登記，
症狀是「改了註解不生效」，而閘門 [3] 照樣報 OK，因為沒登記的來源
不可能不一致：沒有人看它）。

這支程式把來源收斂成一份 `utils/table_semantics.yaml`，並且**在來源就把
句型分開**，讓三個消費端各拿各的投影（見 table_semantics.py 的投影表）。

遷移的正確性怎麼保證
====================================================================
拆散文是會出錯的，而且**錯了不會報**——`_strip_relations()` 就是活生生的
例子（它按「。;；——」切句、丟掉含表名的整句，結果把寫在同一句裡的核心業務
宣告一起吃掉，13 張全中，掉 29~88 字，而 regex 沒報過任何錯）。

所以這裡不靠「看起來拆對了」，靠**重組**：每個子句連同它後面的分隔符一起存，
`compose()` 就是 `decompose()` 的逐字元反函數。驗收條件有兩條，缺一不可：

    ① compose(decompose(c)) == c            對全部 93 張表，逐位元
    ② briefs_for("ddl") == 資料庫現況        三個投影預設全收，所以應該完全相同

②通不過就代表投影表被動過了 —— 重構階段那是 bug，不是改進。

執行：python tools/gen_table_semantics.py            # 預覽，只驗不寫
      python tools/gen_table_semantics.py --write    # 寫出 YAML
"""
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.table_semantics import KINDS, compose  # noqa: E402

OUT = os.path.join(_ROOT, "utils", "table_semantics.yaml")

# 句末分隔符與句內分隔符。切句時**不進括號** —— categories 的註解是
# 「商品分類表（正規化版本；products.category 是反正規化的字串欄位）」，
# 那個分號在括號裡，切下去就把一句話劈成兩半。
_HARD = ("——", "。", "；", ";")
_SOFT = ("，",)
_OPEN, _CLOSE = "（(「『", "）)」』"

_PTR = re.compile(r"在\s*([a-z_]{3,})")
_ENUM = re.compile(r"[A-Z][A-Z_]{2,}(?:/[A-Z][A-Z_]{2,})+")
_USAGE = re.compile(r"(?:用|看)這張表|會用到")
_BOUND = re.compile(r"不是|不同|無關|並存|≠|不等於|不含")


def _split(s: str, seps: tuple[str, ...]) -> list[tuple[str, str]]:
    """切成 [(文字, 後面的分隔符), ...]，括號內不切。無損：接回去等於原字串。"""
    out, buf, depth, i = [], [], 0, 0
    while i < len(s):
        ch = s[i]
        if ch in _OPEN:
            depth += 1
        elif ch in _CLOSE:
            depth = max(0, depth - 1)
        hit = ""
        if depth == 0:
            for sep in seps:
                if s.startswith(sep, i):
                    hit = sep
                    break
        if hit:
            out.append(("".join(buf), hit))
            buf = []
            i += len(hit)
            continue
        buf.append(ch)
        i += 1
    if buf:
        out.append(("".join(buf), ""))
    return out


# 混型子句的明示例外。切句規則**刻意不為此改複雜** —— 「遇到括號就切」會波及
# categories 的「（正規化版本；products.category 是反正規化的字串欄位）」。
# 一個手寫例外看得見、可以被複核，而且往返驗證照樣證明它無損；
# 改規則去自動處理它，會多一條沒人核對過的規則。
_OVERRIDES = {
    # 「不是倉庫」是邊界句、「（倉庫在 warehouses）」是指路標，寫在同一段裡。
    # 不拆的話，投影掉 ptr 會把「不是倉庫」一起帶走。
    ("stores", "不是倉庫（倉庫在 warehouses）"): [
        ("bound", "不是倉庫", ""),
        ("ptr", "（倉庫在 warehouses）", ""),
    ],
}


def matches(seg: str, known: set[str]) -> list[str]:
    """這一段命中了哪些句型（不分優先序）。用來找**混型子句** ——
    優先序會替你做決定，而 ptr 是唯一會被投影掉的一型，所以一個
    「既是指路標又是邊界句」的段落被判成 ptr，就會在投影時把邊界句
    一起帶走。那種段落必須被人看到，不能讓優先序默默決定。"""
    hits = []
    if any(w in known for w in _PTR.findall(seg)):
        hits.append("ptr")
    if _ENUM.search(seg):
        hits.append("enum")
    if _USAGE.search(seg):
        hits.append("usage")
    if seg.lstrip().startswith("這是"):
        hits.append("facet")
    if _BOUND.search(seg):
        hits.append("bound")
    return hits or ["content"]


def classify(seg: str, known: set[str]) -> str:
    """句型判定。**ptr 的優先序最高**，因為它是唯一會被投影掉的一型 ——
    分錯別型今天不痛（三個投影都全收），分錯 ptr 才會真的改到檢索輸入。"""
    if any(w in known for w in _PTR.findall(seg)):
        return "ptr"
    if _ENUM.search(seg):
        return "enum"
    if _USAGE.search(seg):
        return "usage"
    if seg.lstrip().startswith("這是"):
        return "facet"
    if _BOUND.search(seg):
        return "bound"
    return "content"


def decompose(comment: str, known: set[str], table: str = "") -> list[dict]:
    clauses = []
    for sent, hard in _split(comment, _HARD):
        parts = _split(sent, _SOFT)
        for j, (seg, soft) in enumerate(parts):
            sep = soft if j < len(parts) - 1 else hard
            if not seg and not sep:
                continue
            ov = _OVERRIDES.get((table, seg.strip()))
            if ov:
                # 例外的最後一段接手原本的分隔符，其餘接空字串 —— 往返照樣無損
                for i, (k, s, _) in enumerate(ov):
                    clauses.append({"kind": k, "text": s,
                                    "sep": sep if i == len(ov) - 1 else ""})
                continue
            clauses.append({"kind": classify(seg, known), "text": seg, "sep": sep})
    return _merge(clauses)


def _merge(clauses: list[dict]) -> list[dict]:
    """把「同型、且以『，』相接」的相鄰子句併回一句。

    切句是按標點切的，所以「訂單主表：一張訂單一列，記錄下單客戶…」會被切成
    兩句 content —— 往返仍然無損，但這是要給人手改的檔案，碎成 136 句沒人想維護。
    併回去之後往返照樣無損（分隔符被吃進文字裡），而句型邊界一個都沒動。
    """
    out: list[dict] = []
    for c in clauses:
        if out and out[-1]["kind"] == c["kind"] and out[-1]["sep"] == "，":
            out[-1] = {"kind": c["kind"],
                       "text": out[-1]["text"] + "，" + c["text"],
                       "sep": c["sep"]}
        else:
            out.append(dict(c))
    return out


def main() -> int:
    db = get_db_manager(MYSQL_URI)
    with db.engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'")).fetchall()
    live = {t.lower(): (c or "").strip() for t, c in rows}
    known = set(live)
    # 欄位註解一起收進來 —— 一張表的全部資訊放在同一個地方，才是「好維護」。
    # 欄位不分句型（見 table_semantics._read 的說明：量過，沒有依據）。
    with db.engine.connect() as conn:
        colrows = conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION")).fetchall()
    cols: dict[str, dict[str, str]] = {}
    for t, n, cm in colrows:
        cols.setdefault(t.lower(), {})[n.lower()] = (cm or "").strip()
    print(f"資料庫 {len(live)} 張表 / {len(colrows)} 個欄位")

    data, bad = {}, []
    for t, c in sorted(live.items()):
        cl = decompose(c, known, t)
        if compose(cl) != c:
            bad.append(t)
        data[t] = cl

    # ── 驗收 ① 逐位元往返 ────────────────────────────────────────────
    print(f"\n[1] 往返無損   {'OK' if not bad else 'FAIL'}"
          f"（{len(live) - len(bad)}/{len(live)} 逐位元相同）")
    for t in bad[:5]:
        print(f"    ✗ {t}\n      原 {live[t]}\n      回 {compose(data[t])}")
    if bad:
        return 1

    # ── 驗收 ② 預設投影 == 現況 ─────────────────────────────────────
    diff = [t for t in data if compose(data[t], KINDS) != live[t]]
    print(f"[2] 預設投影   {'OK' if not diff else 'FAIL'}"
          f"（三個投影都全收，應與資料庫完全相同）")
    if diff:
        for t in diff[:5]:
            print(f"    ✗ {t}")
        return 1

    # ── 句型分佈 ────────────────────────────────────────────────────
    from collections import Counter
    cnt = Counter(c["kind"] for cl in data.values() for c in cl)
    tabs = {k: sum(1 for cl in data.values() if any(c["kind"] == k for c in cl))
            for k in KINDS}
    print(f"\n句型分佈（子句數 / 有這型的表數）")
    for k in KINDS:
        print(f"    {k:<9} {cnt.get(k, 0):4d} 句   {tabs[k]:3d} 張表")

    # ── 混型子句：優先序替你做了決定，這裡把決定攤開 ──────────────────
    mixed = []
    for t, cl in sorted(data.items()):
        for c in cl:
            hits = matches(c["text"], known)
            if len(hits) > 1:
                mixed.append((t, c["kind"], hits, c["text"].strip()))
    print(f"\n混型子句 {len(mixed)} 句 —— 優先序判給了第一欄，其餘型別被吞掉")
    for t, k, hits, s in mixed:
        warn = "  ⚠ 投影掉 ptr 會一起帶走 " + "/".join(x for x in hits if x != "ptr") \
            if k == "ptr" else ""
        print(f"    {t:<26} {k:<8} {hits}  「{s}」{warn}")

    nocontent = [t for t, cl in data.items() if not any(c["kind"] == "content" for c in cl)]
    if nocontent:
        print(f"\n沒有 content 子句的表 {len(nocontent)} 張：{nocontent}")

    # ptr 是唯一會被投影掉的一型，所以它的判定要能被人核對
    print(f"\nptr 子句全列（唯一會被投影掉的一型，逐句核對）")
    for t in sorted(data):
        p = [c["text"].strip() for c in data[t] if c["kind"] == "ptr"]
        if p:
            print(f"    {t:<26} {p}")

    if "--write" not in sys.argv:
        print(f"\n（預覽模式。加 --write 寫出 {os.path.relpath(OUT, _ROOT)}）")
        return 0

    # ── enum 值域：搬成一等公民，不再從註解措辭推導 ──────────────────
    #
    # 舊做法是 gen_enum_fields.py 讀「資料 + 欄位註解」，用 R1/R2/R3 判斷
    # 「這段註解有沒有把值域講清楚」，講不清楚的才收進 enum_fields ——
    # 那條規則的用意（不要把 DDL 已經有的東西再送一次）是對的，但它讓
    # **值域變成散文措辭的函數**：改一句註解的寫法，就會靜默改變模型
    # 拿不拿得到值域。實測那個耦合已經斷了：
    #
    #   · 分級守衛 `if len(hit) < 2: continue` 要求「註解已提到 >= 2 個值」
    #     才往下走，於是「註解一個值都沒提」——**正好是 R2 的定義**——
    #     的 17 個代碼欄位（lifecycle_stage / to_status / avs_result …）
    #     連分級都沒進到。
    #   · 線上 enum_fields 有 52 欄，而產生器今天重跑得到最小集 0 欄；
    #     產物已經無法從產生鏈重現，而且沒有任何閘門在看（[2] 只驗 DDL）。
    #
    # 值域是 schema 事實，直接宣告。去重不再靠分類器，靠投影。
    from langgraph_sql.utils.schema_registry import get_schema_parser
    ef = get_schema_parser()._data.get("enum_fields") or {}
    enums: dict[str, dict] = {}
    for key, info in ef.items():
        t, _, c = key.lower().partition(".")
        enums.setdefault(t, {})[c] = {
            "description": info.get("description", ""),
            "values": dict(info.get("values") or {}),
        }
    # ── 補漏：分級守衛擋掉的代碼欄位（arm: gap，宣告但預設不送）──────────
    #
    # `gen_enum_fields.py` 的 `if len(hit) < 2: continue` 要求註解**已經**提到
    # 至少 2 個值才往下走，於是「一個值都沒提」——正好是 R2 的定義——的欄位
    # 連分級都沒進到。這裡直接從資料補宣告：值域是 schema 事實。
    # 標 arm: gap，因為送進 Prompt 是行為改變（見 table_semantics.ENUM_GAPS）。
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import gen_enum_fields as gef  # noqa: E402
    _CODE = re.compile(r"^[A-Z][A-Z0-9_]*$")
    n_gap, n_dup, n_rows = 0, 0, {}
    with db.engine.connect() as conn:
        for t, n, ty in conn.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM "
                "INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()")):
            t, n, tl = t.lower(), n.lower(), ty.lower()
            if not tl.startswith("enum") and not (
                    tl.startswith(("varchar", "char")) and gef._width(tl) <= gef.MAX_LEN):
                continue
            if n in enums.get(t, {}):
                continue
            if t not in n_rows:
                n_rows[t] = conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            vals = [r[0] for r in conn.execute(text(
                f"SELECT DISTINCT `{n}` FROM `{t}` WHERE `{n}` IS NOT NULL "
                f"ORDER BY 1 LIMIT {gef.MAX_VALUES + 1}"))]
            if not gef.is_closed_set(vals, n_rows[t]):
                continue
            sval = [str(v) for v in vals]
            if not all(_CODE.match(v) for v in sval):
                continue
            cmt = cols.get(t, {}).get(n, "")
            hit = [v for v in sval
                   if not gef._NUMERIC.match(v) and re.search(gef._token(v), cmt)]
            # arm 只決定「送不送」，不決定「宣告不宣告」。
            #   gap  註解一個值都沒提 —— 模型無從得知，但送進去是行為改變
            #   dup  註解已經列了 —— DDL 就帶著，再送一次是重複
            # 兩者都宣告，因為**漏宣告的欄位不可能被閘門檢查到**，
            # 跟漏登記的來源是同一個病。
            arm = "dup" if len(hit) >= 2 else "gap"
            enums.setdefault(t, {})[n] = {
                "description": cmt, "arm": arm,
                "values": {v: gef.gloss_for(cmt, v) for v in sval},
            }
            n_gap += arm == "gap"
            n_dup += arm == "dup"

    print(f"\nenum：{len(ef)} 個欄位搬進 {len(enums)} 張表"
          f"；另補 {n_gap} 個 arm=gap、{n_dup} 個 arm=dup（宣告但預設不送）")

    out = {t: {"clauses": data[t], "columns": cols.get(t, {}),
               **({"enums": enums[t]} if t in enums else {})}
           for t in sorted(data)}
    with io.open(OUT, "w", encoding="utf-8") as f:
        # sort_keys=False：欄位保持 ORDINAL_POSITION 的順序，人讀起來才對得上 schema
        yaml.safe_dump({"version": 1, "enum_order": list(ef), "tables": out}, f,
                       allow_unicode=True, sort_keys=False, width=10000)
    print(f"\n已寫出 {OUT}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
