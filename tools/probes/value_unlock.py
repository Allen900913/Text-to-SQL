# -*- coding: utf-8 -*-
"""把英文代碼移出註解，值索引會不會接住那 17 題？—— 模擬，不動資料庫

為什麼是模擬
================================================================
`value_index._build()` 的 `schema_text` 直接讀 INFORMATION_SCHEMA，所以
「代碼是否還在註解裡」不是投影旗標能改的事，得真的改資料庫註解。改之前
先在探針裡把同一套規則餵一份**已經拿掉代碼的** schema_text，看結果。
接得住才值得動註解 —— 動了要重跑 sync、重建索引，不是一位元可逆的。

兩份 schema_text
    S0  現況（代碼寫在表註解 enum 句與欄位註解裡）
    S1  把所有大寫代碼 token 從註解裡拿掉之後

`_keep()` 第 ① 條「出現在任何 schema 註解裡的值不收」是唯一的差別來源，
其餘規則（② 純 ASCII 短於 4、`VALUE_MAX_TABLES`、最長匹配）原封不動。

驗收指標：17 題含英文代碼的問句，值索引撈到 GT 表的題數。現況是 0。
"""
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "eval"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from brief_arms import load_cases  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.value_index import MYSQL_URI, VALUE_MAX_TABLES, _keep  # noqa: E402

# 註解裡的英文代碼：連續大寫（可含底線與數字），長度 >= 3。
# 跟 gen_table_semantics 的 `_ENUM` 同一個形狀，只是這裡是用來**移除**。
_CODE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")


def build(schema_text: str, cols, conn):
    acc: dict[str, set[tuple[str, str]]] = {}
    for t, c in cols:
        for (v,) in conn.execute(text(
                f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL")):
            v = str(v).strip()
            if _keep(v, schema_text):
                acc.setdefault(v, set()).add((t, c))
    return {v: frozenset(tc) for v, tc in acc.items()}


# 上限數的是什麼：`pair` 是現行實作（len(tc)，(表,欄位) 對），
# `table` 是 docstring 說的意思（跨幾張表）。差別在 `order_status_history`
# 這種一張表有 from_status／to_status 兩欄的表 —— 同一張表的兩個欄位
# 不是跨表歧義，卻讓每個訂單狀態值都多算一格。
COUNT = os.environ.get("VU_COUNT", "pair")


def _size(tc):
    return len(tc) if COUNT == "pair" else len({t for t, _ in tc})


def hits(idx, question: str) -> dict[str, int]:
    q = question.lower()
    hit = [(v, tc) for v, tc in idx.items()
           if v.lower() in q and _size(tc) <= VALUE_MAX_TABLES]
    hit = [(v, tc) for v, tc in hit
           if not any(v != o and v.lower() in o.lower() for o, _ in hit)]
    out: dict[str, int] = {}
    for _v, tc in hit:
        for t, _c in tc:
            out[t] = out.get(t, 0) + 1
    return out


def main() -> int:
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        raw = ([r[0] or "" for r in conn.execute(text(
            "SELECT TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"))]
            + [r[0] or "" for r in conn.execute(text(
                "SELECT COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE()"))])
        cols = conn.execute(text(
            "SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME) "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND DATA_TYPE IN ('varchar', 'char', 'enum') "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION")).fetchall()

        s0 = " ".join(raw).lower()
        s1 = _CODE.sub(" ", " ".join(raw)).lower()
        gone = sorted({m for c in raw for m in _CODE.findall(c)})
        print(f"註解裡的大寫代碼 token 共 {len(gone)} 種：{'、'.join(gone[:18])}"
              f"{' …' if len(gone) > 18 else ''}\n")

        i0, i1 = build(s0, cols, conn), build(s1, cols, conn)

    print(f"上限計數方式 = {COUNT}（VALUE_MAX_TABLES={VALUE_MAX_TABLES}）")
    print(f"值索引大小　S0 現況 {len(i0)} 個值　→　S1 代碼移出註解 {len(i1)} 個值"
          f"（＋{len(i1) - len(i0)}）")
    new = {v: tc for v, tc in i1.items() if v not in i0}
    wide = {v: tc for v, tc in new.items() if _size(tc) > VALUE_MAX_TABLES}
    print(f"新收的 {len(new)} 個值裡，跨表 > {VALUE_MAX_TABLES} 因而仍不可用的有 {len(wide)} 個\n")

    cases = load_cases(get_table_columns())
    code_qs = [(i, q, n) for i, q, n in cases if _CODE.search(q)]
    print(f"{'題':>5}{'S0':^4}{'S1':^4}  {'S1 命中的值 → 表':<42}{'GT 表':<24}問句")
    print("-" * 124)
    ok0 = ok1 = 0
    for i, q, needs in code_qs:
        gt = set().union(*needs)
        h0, h1 = hits(i0, q), hits(i1, q)
        g0, g1 = bool(gt & set(h0)), bool(gt & set(h1))
        ok0 += g0
        ok1 += g1
        shown = "、".join(f"{t}({n})" for t, n in sorted(
            h1.items(), key=lambda kv: -kv[1])[:3]) or "—"
        print(f"#{i:<4d}{'○' if g0 else '·':^4}{'○' if g1 else '·':^4}  {shown[:40]:<42}"
              f"{','.join(sorted(min(needs, key=len)))[:22]:<24}{q[:22]}")
    print(f"\n含英文代碼的 {len(code_qs)} 題，撈到 GT 表：S0 {ok0} 題 → S1 {ok1} 題")

    # 副作用：其餘 288 題會不會被新收的值帶偏
    other = [(i, q, n) for i, q, n in cases if not _CODE.search(q)]
    noise = [(i, sorted(set(hits(i1, q)) - set(hits(i0, q)))) for i, q, _ in other]
    noisy = [(i, d) for i, d in noise if d]
    print(f"其餘 {len(other)} 題裡，值索引命中表有變的 {len(noisy)} 題"
          + ("" if not noisy else "：" + "、".join(f"#{i}{d}" for i, d in noisy[:8])))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
