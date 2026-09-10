# -*- coding: utf-8 -*-
"""介入的射程 vs 題目的落後量 —— 保留組出題的設計規則。

一題只有在「GT 表落後第一名的餘弦量」落在「這個介入能推動的量」之內時，
才對這個介入有鑑別力。落後太多推不動（判準不可能達成），落後太少是硬幣
（誰贏跟介入無關）。C2 的判準沒過，要先分清楚是哪一種。
"""
import io, json, os, statistics, sys
_R = r"C:\Text-to-SQL"
for p in (_R, os.path.join(_R, "eval"), os.path.join(_R, "tools", "probes")):
    sys.path.insert(0, p)
from loguru import logger as log
log.remove()
import yaml
from brief_arms import CACHE, load_cases, vectors
from langgraph_sql.utils.embedding import cosine
from langgraph_sql.utils.schema_registry import get_table_columns
from langgraph_sql.utils.table_filter import get_table_briefs
from langgraph_sql.utils.table_semantics import KINDS, compose, load

KIND = sys.argv[1] if len(sys.argv) > 1 else "ptr"
SPEC = sys.argv[2] if len(sys.argv) > 2 else "holdout_ptr"
NOPTR = tuple(k for k in KINDS if k != KIND)
cache = json.load(io.open(CACHE, encoding="utf-8"))
d = load(); base = get_table_briefs()
arm = {t: compose(cl, NOPTR) for t, cl in d.items()}
A, X = vectors(base, cache), vectors(arm, cache)
CH = [t for t in base if base[t] != arm[t]]

spec = yaml.safe_load(io.open(os.path.join(_R, "tools", "probes", SPEC + ".yaml"),
                              encoding="utf-8"))["groups"]
qs = [(g, i["q"], i["want"]) for g in sorted(spec) for i in spec[g]]
qv = cache["__%s__" % SPEC]

# 射程：ptr 句被拿掉的 13 張表，餘弦被推動多少（用全庫 305 個問句量）
moves = []
for v in cache["__queries__"]:
    for t in CH:
        moves.append(abs(cosine(v, A[t]) - cosine(v, X[t])))
ms = sorted(moves)
print(f"-{KIND} 的射程（{len(CH)} 張改寫表 × {len(cache[chr(95)*2+chr(113)+chr(117)+chr(101)+chr(114)+chr(105)+chr(101)+chr(115)+chr(95)*2])} 問句 的 |Δcos|）")
print(f"   中位 {ms[len(ms)//2]:.4f}　p75 {ms[int(len(ms)*.75)]:.4f}"
      f"　p95 {ms[int(len(ms)*.95)]:.4f}　最大 {ms[-1]:.4f}\n")
reach = ms[int(len(ms) * .95)]

print(f"{'組':<4}{'期望表':<24}{'A名次':>6}{'落後量':>9}{'射程內':>7}  {'X名次':>8}")
print("-" * 76)
tab = {}
for (g, q, want), v in zip(qs, qv):
    sc = sorted(((cosine(v, x), t) for t, x in A.items()), reverse=True)
    ra = [t for _, t in sc].index(want) + 1
    deficit = sc[0][0] - cosine(v, A[want])
    rx = sorted(X, key=lambda t: -cosine(v, X[t])).index(want) + 1
    inreach = deficit <= reach
    t = tab.setdefault(g, {"n": 0, "miss": 0, "miss_reach": 0})
    t["n"] += 1
    if ra > 1:
        t["miss"] += 1; t["miss_reach"] += inreach
        print(f"{g:<4}{want:<24}{ra:6d}{deficit:9.4f}{'○' if inreach else '✗':>6}"
              f"{rx:9d}")
print()
for g in sorted(tab):
    t = tab[g]
    print(f"  {g:<4}{t['n']:3d} 題　A 臂失手 {t['miss']:2d} 題　其中落後量在射程內的 {t['miss_reach']:2d} 題")
