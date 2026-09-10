# -*- coding: utf-8 -*-
"""表註解沒寫某個欄位資訊，會不會導致「檢索不到」？—— 量餘裕，不是量命中。

為什麼要單獨量這個
================================================================
三層都在送欄位語意，但**只有第一層會淘汰表**：

    ① 向量檢索（retrieval 投影）   93 張表 → 候選 CANDIDATE_N 張
    ② 候選目錄的欄位提示           只排序，不淘汰
    ③ 值索引證據                   只排序，不淘汰

②③ 看不到 ① 沒選進候選的表。所以「表註解漏寫欄位語意」的風險**全部**
集中在 ①，而且它掉了不會報錯（§7.2）。

量什麼
================================================================
不是「命中率多少」—— 那個數字現在很好看，會掩蓋問題。要量的是**餘裕**：
每題的 GT 表排第幾名，離候選門檻還有多遠。餘裕薄的那些題，才是
「再從表註解拿掉一句話就會掉出去」的題。

輸出：
    [1] 分布    GT 表名次的分位數 ＋ 落在候選外的題
    [2] 薄冰    名次 > CANDIDATE_N/2 的題（離門檻不到一半餘裕）
    [3] 歸屬    薄冰題的 GT 表，各自的表註解長度與句型組成
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "eval"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

from brief_arms import CACHE, load_cases, vectors     # noqa: E402
from langgraph_sql.utils.embedding import cosine, embed, doc_hash  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.table_filter import get_candidate_n  # noqa: E402
from langgraph_sql.utils.table_semantics import load, briefs_for  # noqa: E402


def main() -> int:
    import json
    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    cases = load_cases(get_table_columns())
    tv = briefs_for("retrieval")
    A = vectors(tv, cache)
    qs = [q for _, q, _ in cases]
    stale = [q for q in qs if doc_hash("Q:" + q) not in cache]
    if stale:
        for q, v in zip(stale, embed(stale, "query")):
            cache[doc_hash("Q:" + q)] = v
    QV = {q: cache[doc_hash("Q:" + q)] for q in qs}
    N = get_candidate_n()
    print(f"題數 {len(cases)}　表 {len(A)}　候選上限 CANDIDATE_N={N}\n")

    rows = []
    for qid, q, needs in cases:
        v = QV[q]
        rank = [t for t, _ in sorted(((t, cosine(v, x)) for t, x in A.items()),
                                     key=lambda p: (-p[1], p[0]))]
        pos = {t: i + 1 for i, t in enumerate(rank)}
        # 一題可能有多組可接受的 GT 表；取最容易的那一組，再取組內最差的名次
        worst = min(max(pos.get(t, 10**6) for t in nd) for nd in needs if nd)
        rows.append((qid, q, worst, needs))

    rows.sort(key=lambda r: -r[2])
    ranks = sorted(r[2] for r in rows)
    n = len(ranks)
    def pct(p): return ranks[min(n - 1, int(n * p))]
    out = [r for r in rows if r[2] > N]
    print(f"[1] GT 表名次分布　中位 {pct(.5)}　p75 {pct(.75)}　p90 {pct(.9)}　"
          f"p95 {pct(.95)}　最差 {ranks[-1]}")
    print(f"    落在候選外（名次 > {N}）：{len(out)} 題"
          + ("" if not out else "　" + ", ".join(f"#{r[0]}" for r in out[:10])))

    thin = [r for r in rows if N // 2 < r[2] <= N]
    print(f"\n[2] 薄冰（名次 {N//2+1}~{N}）：{len(thin)} 題")
    for qid, q, w, needs in thin:
        print(f"    #{qid:<4} 名次 {w:>3}　{q[:38]}")
        print(f"          GT {sorted(set().union(*needs))}")

    if thin or out:
        print(f"\n[3] 薄冰題的 GT 表 —— 表註解長度與句型")
        from langgraph_sql.utils.table_semantics import load as _cl
        d = _cl()
        seen = set()
        for _, _, _, needs in out + thin:
            for t in sorted(set().union(*needs)):
                if t in seen:
                    continue
                seen.add(t)
                kinds = [c["kind"] for c in d.get(t, [])]
                print(f"    {t:28} {len(tv.get(t,'')):>4} 字元　{kinds}")
                print(f"    {'':28} {tv.get(t,'')[:150]}")

    print()
    print("[4] 落在候選外的題 —— 逐表名次")
    for qid, q, w, needs in out:
        v = QV[dict((r[0], r[1]) for r in rows)[qid]]
        sc = sorted(((t, cosine(v, x)) for t, x in A.items()), key=lambda p: (-p[1], p[0]))
        pos = {t: i + 1 for i, (t, _) in enumerate(sc)}
        print(f"    #{qid}　{q}")
        for nd in needs:
            print("      組 " + "  ".join(f"{t}#{pos[t]}" for t in sorted(nd)))
        print("      前 5 名 " + ", ".join(f"{t}#{i+1}" for i, (t, _) in enumerate(sc[:5])))
    with io.open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
