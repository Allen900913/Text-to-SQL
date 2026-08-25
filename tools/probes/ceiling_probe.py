# -*- coding: utf-8 -*-
"""一表一票的資訊天花板：拿欄位註解當問句，它自己的表排得到前 40 嗎？（零 LLM）

使用者提的測法是「直接出題目，問一個大表裡沒被表註解涵蓋的概念，就測得出來」。
這支腳本是那個測法的**全覆蓋、零成本版本**：不用手寫題目，直接把 869 個欄位註解
各當成一句問句，量它所屬的表在 93 張裡排第幾。

為什麼這是有效的代理：**檢索文件就是表註解本身**。如果某個概念沒寫進表註解，
那麼問到那個概念的問句，在檢索層就沒有東西可以匹配。欄位註解是「問到這個欄位」
所能寫出的**最貼題的措辭**，所以：

  · 連自己欄位的註解都排不進前 40 → 真實問句只會更糟（保守下界）
  · 排得進去 → 這個概念在檢索層沒有天花板問題

這會直接檢驗我先前的結論「檢索層不用解」。那個結論是從九題誘餌量出來的，
而**那九題的概念都寫在表註解裡** —— 樣本本身就偏了。

零 LLM，只呼叫嵌入 API（欄位註解要用 'query' 側嵌入，與 col_probe.py 的
'passage' 側快取不能共用）。
"""
import json
import os
import sys

import numpy as np

_ROOT = r"C:\Text-to-SQL"
for p in (_ROOT, os.path.join(_ROOT, "eval"), os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, p)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.table_filter import get_candidate_n, get_table_briefs  # noqa: E402
from langgraph_sql.utils.table_retriever import _embed, get_table_vectors  # noqa: E402

QCACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "col_query_vecs.json")


def col_comments():
    """{(表, 欄): 註解}，排除 id / *_id（不帶語意，當問句沒有意義）。"""
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        db = c.execute(text("SELECT DATABASE()")).scalar()
        rows = c.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=:d ORDER BY TABLE_NAME, ORDINAL_POSITION"), {"d": db}).fetchall()
    return {(t, col): (cm or "").strip() for t, col, cm in rows
            if col != "id" and not col.endswith("_id") and (cm or "").strip()}


def cached_query_vecs(texts: dict):
    if os.path.exists(QCACHE):
        raw = json.load(open(QCACHE, encoding="utf-8"))
        if len(raw) == len(texts):
            return {tuple(k.split("\t")): v for k, v in raw.items()}
    keys = sorted(texts)
    out = {}
    B = 96
    for i in range(0, len(keys), B):
        chunk = keys[i:i + B]
        for k, v in zip(chunk, _embed([texts[k] for k in chunk], "query")):
            out[k] = v
        print(f"    嵌入 {min(i + B, len(keys))}/{len(keys)}", flush=True)
    json.dump({"\t".join(k): v for k, v in out.items()},
              open(QCACHE, "w", encoding="utf-8"))
    return out


def main():
    log.remove()
    cmts = col_comments()
    briefs = get_table_briefs()
    tvecs = get_table_vectors()
    n_cand = get_candidate_n(len(tvecs))
    print(f"欄位註解 {len(cmts)} 份｜表 {len(tvecs)} 張｜候選 {n_cand} 張")

    qv = cached_query_vecs(cmts)
    names = sorted(tvecs)
    M = np.array([tvecs[t] for t in names], dtype=np.float32)
    M /= np.linalg.norm(M, axis=1, keepdims=True)
    idx = {t: i for i, t in enumerate(names)}

    keys = sorted(qv)
    Q = np.array([qv[k] for k in keys], dtype=np.float32)
    Q /= np.linalg.norm(Q, axis=1, keepdims=True)
    S = Q @ M.T                                   # (欄位, 表)
    order = np.argsort(-S, axis=1)
    ranks = {}
    for r, k in enumerate(keys):
        own = idx[k[0]]
        ranks[k] = int(np.where(order[r] == own)[0][0]) + 1

    vals = np.array(list(ranks.values()))
    wide = {k: v for k, v in ranks.items() if k[0].endswith("_profiles")}
    narrow = {k: v for k, v in ranks.items() if not k[0].endswith("_profiles")}

    def line(tag, d):
        a = np.array(list(d.values()))
        out = int((a > n_cand).sum())
        return (f"  {tag:14s} {len(a):>4d} 欄｜中位 {int(np.median(a)):>3d}｜"
                f"第 90 百分位 {int(np.percentile(a, 90)):>3d}｜"
                f"排不進前 {n_cand} 的 {out:>3d} 欄 = {out / len(a):5.1%}")

    print("\n把欄位註解當問句，它自己的表排第幾（93 張表中）：")
    print(line("全部", ranks))
    print(line("寬表 profile", wide))
    print(line("其餘 79 張", narrow))

    print(f"\n落在候選之外（> {n_cand}）最嚴重的 20 欄 —— "
          f"這些概念**問了就檢索不到**：")
    worst = sorted(ranks.items(), key=lambda kv: -kv[1])[:20]
    for (t, c), r in worst:
        print(f"    {r:>3d}/93  {t}.{c:26s}「{cmts[(t, c)][:26]}」")

    print("\n逐表：有幾個欄位的概念排不進候選（只列有問題的）")
    per = {}
    for (t, c), r in ranks.items():
        per.setdefault(t, []).append(r)
    bad = [(t, sum(1 for x in rs if x > n_cand), len(rs)) for t, rs in per.items()]
    for t, nbad, n in sorted(bad, key=lambda x: -x[1])[:14]:
        if not nbad:
            continue
        print(f"    {t:26s} {nbad:>3d}/{n:<3d} 欄排不進前 {n_cand}"
              f"（表註解 {len(briefs.get(t, ''))} 字）")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
