# -*- coding: utf-8 -*-
"""E14：代碼移出欄位註解，欄位提示那一層會不會變差？（確定性，零 LLM）

欄位提示（`COLUMN_HINT_K=2`）把 869 份欄位文件嵌入，替候選目錄裡的每張表
列出「與這一題最像的 2 個欄位」。文件格式是
`{表註解前 18 字} · {欄位名}（{欄位註解}）`，所以**欄位註解也餵檢索** ——
這一層是 E15 先前沒量到的成本面。

78/869 份文件會變，其餘 791 份原封不動 → 大多數題目是免費的雜訊地板
（memory: `single-layer-interventions-make-their-own-control`）。

指標：GT SQL 真正用到的欄位，有沒有被提示列出來。
    覆蓋率 = 被提示命中的 (題, GT 欄位) 對 / 全部 (題, GT 欄位) 對
只算 k=2 之內，因為那就是模型實際看到的。

兩段跑法：`--baseline` 先印基線，登記判準之後再跑一次。
"""
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "eval"), os.path.join(_ROOT, "tools"),
          os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from brief_arms import load_cases  # noqa: E402
from langgraph_sql.utils.column_hints import HINT_K, _load  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.embedding import cosine, embed  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.value_index import MYSQL_URI  # noqa: E402
from strip_enum_codes import should_strip, strip  # noqa: E402

from eval_schema_need import required_schema  # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".hint_strip_cache.json")

# ── 事前登記的判準（看完基線、未看處理臂之後填）─────────────────────
# 這一層的介入是**移除文字**，而移除的是英文代碼 —— 對中文問句的餘弦
# 應該幾乎沒有影響。所以這是一個「證明無害」的測試，不是「證明有益」：
CRITERIA = None   # 跑 --baseline 之後填


def vecs(docs, cache):
    stale = [d for d in set(docs.values()) if d not in cache]
    if stale:
        print(f"    嵌入 {len(stale)} 份欄位文件…")
        for i in range(0, len(stale), 96):
            ch = stale[i:i + 96]
            for d, v in zip(ch, embed(ch, "passage")):
                cache[d] = v
    return {k: cache[d] for k, d in docs.items()}


def main() -> int:
    docs, cmts = _load()
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        cols = conn.execute(text(
            "SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE IN ('varchar','char','enum')")).fetchall()
        V = {}
        for t, c in cols:
            V[(t, c)] = {str(v).strip() for (v,) in conn.execute(
                text(f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL"))}

    arm = {}
    changed = []
    for (t, c), d in docs.items():
        cm = cmts[(t, c)]
        if should_strip(t, c, cm, V.get((t, c), set())):
            new = strip(cm, V.get((t, c), set()))
            arm[(t, c)] = d.replace(f"（{cm}）", f"（{new}）")
            changed.append((t, c))
        else:
            arm[(t, c)] = d
    print(f"欄位文件 {len(docs)} 份，改寫 {len(changed)} 份\n")

    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    A, X = vecs(docs, cache), vecs(arm, cache)
    cases = load_cases(get_table_columns())
    known = get_table_columns()
    qk = "__q__"
    if cache.get(qk + "n") != len(cases):
        print(f"    嵌入 {len(cases)} 個問句…")
        cache[qk] = embed([q for _, q, _ in cases], "query")
        cache[qk + "n"] = len(cases)
    qv = cache[qk]
    with io.open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)

    def topk(tv, v, table):
        cs = [(cosine(v, x), c) for (t, c), x in tv.items() if t == table]
        return {c for _s, c in sorted(cs, reverse=True)[:HINT_K]}

    tot = {"A": 0, "X": 0}
    n = 0
    diff = []
    import yaml
    gtf = yaml.safe_load(io.open(os.path.join(_ROOT, "eval_ground_truth.yaml"),
                                 encoding="utf-8"))
    sqlmap = {e["id"]: e.get("sql") for e in gtf}
    for (qid, q, needs), v in zip(cases, qv):
        sql = sqlmap.get(qid)
        if not sql:
            continue
        try:
            _t, gcols = required_schema(sql, known)
        except Exception:
            continue
        for f in gcols:
            t, _, c = f.partition(".")
            if (t, c) not in docs:
                continue                       # 主鍵外鍵與無註解欄位不在提示池裡
            n += 1
            a, x = c in topk(A, v, t), c in topk(X, v, t)
            tot["A"] += a
            tot["X"] += x
            if a != x:
                diff.append((qid, f, a, x, q))

    print(f"(題, GT 欄位) 對 {n} 組　k={HINT_K}")
    print(f"  A 現行      命中 {tot['A']:4d}　{tot['A'] / n * 100:.1f}%")
    print(f"  X 代碼移出  命中 {tot['X']:4d}　{tot['X'] / n * 100:.1f}%　"
          f"淨 {tot['X'] - tot['A']:+d}")
    print(f"\n命中狀態改變的 {len(diff)} 組：")
    for qid, f, a, x, q in diff:
        print(f"  #{qid:<4d}{f:<44}{'命中→漏' if a else '漏→命中'}   {q[:26]}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
