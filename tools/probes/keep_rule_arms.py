# -*- coding: utf-8 -*-
"""`_keep` 規則① 的三個臂 —— 零 LLM、確定性，決定值不值得跑 309 題回測。

為什麼不能直接跑 309 題
====================================================================
開發集現況 307/309 = 99.4%。規則① 的改動如果只推得動個位數的題，
單輪回測分不出「掉 1 題」是效應還是雜訊（[[eval-noise-single-run]]、
[[retrieval-is-nondeterministic]]：固定程式碼重跑 20.5% 的題會選到不同的表）。
射程要先量在**它真的動得到的那一層**：值命中集合與 dense 加權。

三個臂
====================================================================
    A  現況      value.lower() not in schema_text      （規則① 全開）
    B  全拆      規則① 完全移除                        （實驗二）
    C  實體比對  只擋 value.lower() ∈ {表名} ∪ {欄位名} （實驗三）

② 純 ASCII 短於 4、`_MIN_LEN`、`VALUE_MAX_TABLES`、最長匹配 —— 三臂相同。

指標
====================================================================
    · 索引規模與新進值的落點（住 ENUM 欄的，DDL 本來就送得到）
    · 309 題裡「值命中集合改變」的題數 ← 這就是射程
    · 新命中指向 GT 表 / 非 GT 表的次數 ← 訊號 vs 雜訊的比例
    · dense 加權被推動的表數（VALUE_BETA·min(n,3)）

用法：python tools/probes/keep_rule_arms.py
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "eval")):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from eval_schema_need import required_schema  # noqa: E402
from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.value_index import (  # noqa: E402
    VALUE_MAX_TABLES, _MIN_LEN,
)

GT_PATH = os.path.join(_ROOT, "eval_ground_truth.yaml")


def _base_keep(v: str) -> bool:
    """三臂共用的部分 —— 規則① 以外的一切。"""
    if len(v) < _MIN_LEN:
        return False
    if v.isascii() and len(v) < 4:
        return False
    return True


def collect():
    """掃一次資料庫，回傳 (值 -> {(表,欄)}, schema_text, 實體名集合, enum欄集合)。"""
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        schema_text = " ".join(
            [r[0] or "" for r in conn.execute(text(
                "SELECT TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE()"))]
            + [r[0] or "" for r in conn.execute(text(
                "SELECT COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE()"))]).lower()
        cols = conn.execute(text(
            "SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME), DATA_TYPE "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND DATA_TYPE IN ('varchar', 'char', 'enum') "
            "ORDER BY TABLE_NAME, ORDINAL_POSITION")).fetchall()
        names = {t for t, _c, _d in cols} | {c for _t, c, _d in cols}
        names |= {r[0].lower() for r in conn.execute(text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"))}
        names |= {r[0].lower() for r in conn.execute(text(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()"))}
        enum_cols = {(t, c) for t, c, d in cols if d == "enum"}
        raw = {}
        for t, c, _d in cols:
            for (v,) in conn.execute(text(
                    f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL")):
                raw.setdefault(str(v).strip(), set()).add((t, c))
    return raw, schema_text, names, enum_cols


def build(raw, pred):
    return {v: frozenset(tc) for v, tc in raw.items() if pred(v)}


def matches(idx, question: str):
    q = question.lower()
    hit = [(v, tc) for v, tc in idx.items()
           if v.lower() in q and len({t for t, _ in tc}) <= VALUE_MAX_TABLES]
    return [(v, tc) for v, tc in hit
            if not any(v != other and v.lower() in other.lower()
                       for other, _ in hit)]


def main() -> int:
    raw, st, names, enum_cols = collect()
    arms = {
        "A 現況":   build(raw, lambda v: _base_keep(v) and v.lower() not in st),
        "B 全拆":   build(raw, _base_keep),
        "C 實體比對": build(raw, lambda v: _base_keep(v) and v.lower() not in names),
    }
    print("掃過 %d 個相異值\n" % len(raw))
    for name, idx in arms.items():
        print("  %-10s %5d 個值進索引" % (name, len(idx)))

    A, B, C = arms["A 現況"], arms["B 全拆"], arms["C 實體比對"]
    gained = sorted(set(B) - set(A))
    print("\n規則① 目前擋掉 %d 個值。C 臂會擋掉其中的 %d 個：\n"
          % (len(gained), len([v for v in gained if v not in C])))
    for v in gained:
        tcs = sorted(raw[v])
        in_enum = all(tc in enum_cols for tc in tcs)
        print("   %-14s %s%-6s %s" % (
            v, "C擋" if v not in C else "C放", "｜ENUM" if in_enum else "｜VARCHAR",
            ", ".join("%s.%s" % tc for tc in tcs)[:70]))

    # ---------------- 射程：309 題的值命中集合 ----------------
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    known = get_table_columns()
    cases = []
    for e in gt:
        if e.get("expect") == "schema_unsupported" or not e.get("sql"):
            continue
        need = set()
        for sql in [e["sql"]] + list(e.get("alt_sql") or []):
            try:
                n, _ = required_schema(sql, known)
            except Exception:
                continue
            if n:
                need |= set(n)
        cases.append((e["id"], e["question"], need))

    print("\n開發集 %d 題（扣掉拒答題）—— 值命中集合的差異" % len(cases))
    for label, idx in (("B 全拆", B), ("C 實體比對", C)):
        changed, sig, noise, detail = 0, 0, 0, []
        for qid, q, need in cases:
            a = {(v, tc) for v, tc in matches(A, q)}
            x = {(v, tc) for v, tc in matches(idx, q)}
            if a == x:
                continue
            changed += 1
            new = x - a
            for v, tc in sorted(new):
                ts = {t for t, _ in tc}
                if ts & need:
                    sig += 1
                else:
                    noise += 1
            detail.append((qid, q[:26], sorted({v for v, _ in new}),
                           sorted({t for _v, tc in new for t, _ in tc})))
        print("\n  %s：%d 題命中集合改變（射程 = %.1f%%）" % (
            label, changed, 100.0 * changed / len(cases)))
        print("      新命中指向 GT 表 %d 次、指向非 GT 表 %d 次" % (sig, noise))
        for qid, q, vs, ts in detail[:40]:
            mark = "✓" if set(ts) & {t for _, _q, n in cases if _ == qid for t in n} else " "
            print("      #%-5s %-28s %s → %s" % (qid, q, vs, ts))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
