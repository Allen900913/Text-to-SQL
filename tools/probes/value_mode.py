# -*- coding: utf-8 -*-
"""值索引的兩種接法：β 加分 vs 候選聯集（零 LLM，確定性）

為什麼要這一支
====================================================================
值索引現在有兩個接點：dense 層 `+β·min(hits,3)`，與目錄層的事實證據行。
第二個接點（把命中的值當文字放進 context）是 BRIDGE / CHESS 那一系的主流做法。
第一個接點不屬於任何一系 —— 它是把值訊號當成「訓練過的 ranker 的離散特徵」
（RAT-SQL / RESDSQL 那一系）在用，但用手設常數代替學出來的權重。

而 β 實際上做得到的事比看起來少。`filter_tables_union` →
`format_catalog(candidates, shuffle_seed, query)` **會把候選順序打散**，
所以 dense 分數在候選集合內部的排序根本沒送到 LLM 面前。β 只剩三件事：

    ① 這張表有沒有跨進 ranked[:CANDIDATE_N]   ← 純二元的成員資格
    ② 誰是 ranked[0]                          ← ∪Top-1 那道保險的錨點
    ③ LLM 整層失效時 pick_anchors 的退路

① 是二元的，聯集可以直接表達，而且不需要一個綁在今天這個嵌入模型餘弦分佈
（Top-1 落在 0.29~0.42）上的尺度常數 —— 換嵌入模型或註解普遍變長變短，
0.05 就代表不同的東西，**而且不會報錯**（§8② 靜默失敗）。

② 更值得量：∪Top-1 的設計理由是**誤差獨立**（餘弦 vs LLM 推理，機制不同）。
β 讓那個本來純餘弦的錨點變成「餘弦＋值」的混合訊號。
**「Top-1 被值命中換掉的題數」就是這道保險被稀釋的量，而它從來沒有人量過。**

兩個臂
====================================================================
    beta   候選 = rank(cosine + β·min(h,3))[:N]      錨點 = 該排序的第 1 名
    union  候選 = rank(cosine)[:N] ∪ {值命中的表}     錨點 = 純餘弦第 1 名

union 的代價要老實講：候選不再是固定 N，會變成 N+k。這一支把 k 印出來。

用法：
    python tools/probes/value_mode.py
    python tools/probes/value_mode.py --beta 0.02 0.05 0.10
"""
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "eval"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from loguru import logger as log  # noqa: E402

from brief_arms import CACHE, load_cases, vectors  # noqa: E402
from langgraph_sql.utils.embedding import cosine  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.table_filter import CANDIDATE_N, get_table_briefs  # noqa: E402
from langgraph_sql.utils.value_index import value_hits  # noqa: E402

WATCH = ("products", "order_items", "orders", "customers")


def main() -> int:
    betas = [0.02, 0.05, 0.10]
    if "--beta" in sys.argv:
        betas = [float(x) for x in sys.argv[sys.argv.index("--beta") + 1:]]

    briefs = get_table_briefs()
    cases = load_cases(get_table_columns())
    cache = json.load(io.open(CACHE, encoding="utf-8"))
    qvecs = cache["__queries__"]
    assert len(qvecs) == len(cases), "問句快取與題數對不上，先跑 brief_arms.py"
    tvecs = vectors(briefs, cache)

    base = [dict((t, cosine(qv, v)) for t, v in tvecs.items()) for qv in qvecs]
    hits = [value_hits(q) for _, q, _ in cases]
    nhit = sum(1 for h in hits if h)
    print("題數 %d；候選上限 %d；有值命中的題 %d（%.1f%%）"
          % (len(cases), CANDIDATE_N, nhit, nhit / len(cases) * 100))

    def rank(sc):
        return [t for t, _ in sorted(sc.items(), key=lambda p: (-p[1], p[0]))]

    pure = [rank(s) for s in base]

    def score(name, cands, tops, extra):
        rec = sum(1 for (_, _, nd), c in zip(cases, cands)
                  if any(n <= c for n in nd)) / len(cases) * 100
        top1 = sum(1 for (_, _, nd), t in zip(cases, tops)
                   if any(t in n for n in nd)) / len(cases) * 100
        swapped = sum(1 for t, p in zip(tops, pure) if t != p[0])
        # 被需要時掉出候選的表（閘門 [12] 的紅燈條件，改用集合判）
        out = {}
        for (qid, _, nd), c in zip(cases, cands):
            best = min(nd, key=lambda n: len(n - c))
            for t in best - c:
                out.setdefault(t, []).append(qid)
        return {"name": name, "rec": rec, "top1": top1, "swap": swapped,
                "out": out, "extra": extra}

    rows = []
    for b in betas:
        mod = [dict((t, s + b * min(h.get(t, 0), 3)) for t, s in sc.items())
               for sc, h in zip(base, hits)]
        r = [rank(s) for s in mod]
        rows.append(score("beta β=%.2f" % b, [set(x[:CANDIDATE_N]) for x in r],
                          [x[0] for x in r], 0.0))

    ucands, uextra = [], []
    for p, h in zip(pure, hits):
        top = set(p[:CANDIDATE_N])
        add = set(h) - top
        ucands.append(top | add)
        uextra.append(len(add))
    rows.append(score("union（餘弦不動）", ucands, [p[0] for p in pure],
                      sum(uextra) / len(uextra)))
    rows.insert(0, score("純餘弦（無值索引）", [set(p[:CANDIDATE_N]) for p in pure],
                         [p[0] for p in pure], 0.0))

    head = "%-22s%12s%10s%14s%12s" % ("臂", "候選召回@40", "Top-1", "Top-1被換掉", "多帶候選")
    print("\n" + head)
    print("-" * len(head))
    for r in rows:
        print("%-22s%11.1f%%%9.1f%%%12d 題%11.2f 張"
              % (r["name"], r["rec"], r["top1"], r["swap"], r["extra"]))

    print("\n逐表：被 GT 需要時掉出候選的表（閘門 [12] 紅燈）")
    for r in rows:
        if not r["out"]:
            print("  %-22s 無" % r["name"])
        for t, qs in sorted(r["out"].items(), key=lambda p: -len(p[1])):
            print("  %-22s %-18s %d 題 %s" % (r["name"], t, len(qs), qs[:6]))

    print("\n觀察表在各臂的最差排名（純餘弦排序；union 不改排序，所以與純餘弦同）")
    for t in WATCH:
        worst = 0
        for (qid, _, nd), p in zip(cases, pure):
            if any(t in n for n in nd):
                worst = max(worst, p.index(t) + 1)
        print("  %-14s 純餘弦最差 %3d" % (t, worst))

    print("\n註：union 的錨點與純餘弦完全相同 —— 這就是重點："
          "\n    它把值訊號放進候選集合，而不放進那個要保持誤差獨立的排序。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
