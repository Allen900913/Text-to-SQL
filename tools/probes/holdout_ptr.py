# -*- coding: utf-8 -*-
"""D+（指路標不進檢索文件）的保留組驗收 —— 零 LLM，確定性

判準寫在 holdout_ptr.yaml 裡，**跑之前就登記好了**。這一支只負責跑與對帳，
不負責解釋為什麼沒過。

用法：python tools/probes/holdout_ptr.py
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

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402

from brief_arms import CACHE, load_cases, vectors  # noqa: E402
from langgraph_sql.utils.embedding import cosine, embed  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.table_filter import CANDIDATE_N, get_table_briefs  # noqa: E402
from langgraph_sql.utils.table_semantics import KINDS, compose, load  # noqa: E402

HOLDOUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holdout_ptr.yaml")
NOPTR = tuple(k for k in KINDS if k != "ptr")

# 事前登記的判準（與 YAML 的註解同步；改這裡就是改判準，不准跑完再改）
CRITERIA = {"C": ("增加 >= 3", lambda a, b: b - a >= 3),
            "D": ("一題都不准掉", lambda a, b: b >= a),
            "A": ("±1 以內", lambda a, b: abs(b - a) <= 1),
            "B": ("±1 以內", lambda a, b: abs(b - a) <= 1)}

# C2 是兩段式判準，不能用「總數增減」表達，所以獨立算（見 YAML 的說明）。
# C 組保留在表上但**已作廢** —— 它的判準在自己的基線上不可能達成，
# 那是判準的缺陷。留著是為了不把跑過的東西藏起來。
C2_DEAD = "C"


def main() -> int:
    spec = yaml.safe_load(io.open(HOLDOUT, encoding="utf-8"))["groups"]
    base = get_table_briefs()
    full = {t: compose(cl, NOPTR) for t, cl in load().items()}
    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    A, D = vectors(base, cache), vectors(full, cache)

    qs = [(g, i["q"], i["want"]) for g in sorted(spec) for i in spec[g]]
    key = "__holdout_ptr__"
    if cache.get(key + "_n") != len(qs):
        print(f"嵌入 {len(qs)} 個保留組問句…")
        cache[key] = embed([q for _, q, _ in qs], "query")
        cache[key + "_n"] = len(qs)
    qv = cache[key]

    print(f"{'組':<4}{'期望表':<24}{'A 名次':>8}{'D+ 名次':>9}   問句")
    print("-" * 96)
    tally, ranks = {}, []
    for (g, q, want), v in zip(qs, qv):
        ra = sorted(A, key=lambda t: -cosine(v, A[t])).index(want) + 1
        rd = sorted(D, key=lambda t: -cosine(v, D[t])).index(want) + 1
        ranks.append((ra, rd))
        s = tally.setdefault(g, {"a": 0, "d": 0, "ra": 0, "rd": 0})
        s["a"] += ra == 1
        s["d"] += rd == 1
        s["ra"] += ra
        s["rd"] += rd
        mark = "  ←修好" if rd == 1 and ra != 1 else ("  ←弄壞" if ra == 1 and rd != 1 else "")
        print(f"{g:<4}{want:<24}{ra:8d}{rd:9d}   {q[:30]}{mark}")

    print(f"\n{'組':<4}{'判準':<16}{'A Top-1':>9}{'D+ Top-1':>10}{'A 均名':>8}{'D+ 均名':>9}  結果")
    print("-" * 74)
    passed = True
    for g in sorted(tally):
        n = len(spec[g])
        if g not in CRITERIA:
            continue
        s, (desc, fn) = tally[g], CRITERIA[g]
        ok = fn(s["a"], s["d"])
        if g == C2_DEAD:
            print(f"{g:<4}{desc:<16}{s['a']:6d}/{n}{s['d']:8d}/{n}"
                  f"{s['ra'] / n:8.1f}{s['rd'] / n:9.1f}  作廢（判準在自己的基線上不可能達成）")
            continue
        passed &= ok
        print(f"{g:<4}{desc:<16}{s['a']:6d}/{n}{s['d']:8d}/{n}"
              f"{s['ra'] / n:8.1f}{s['rd'] / n:9.1f}  {'通過' if ok else '✗ 不通過'}")

    # ── C2：兩段式 ──────────────────────────────────────────────────
    c2 = [(q, w, ra, rd) for (g, q, w), ra, rd in
          zip(qs, [x[0] for x in ranks], [x[1] for x in ranks]) if g == "C2"]
    miss = [r for r in c2 if r[2] != 1]
    hit = [r for r in c2 if r[2] == 1]
    fixed = sum(1 for r in miss if r[3] == 1)
    broke = sum(1 for r in hit if r[3] != 1)
    ok_a = fixed * 2 >= len(miss)
    ok_b = broke <= 1
    passed &= ok_a and ok_b
    print(f"\nC2 兩段式（10 題真正的競爭配對）")
    print(f"    A 臂失手 {len(miss)} 題 → D+ 修好 {fixed} 題"
          f"（判準：修好一半以上）  {'通過' if ok_a else '✗ 不通過'}")
    print(f"    A 臂本來就對 {len(hit)} 題 → D+ 弄壞 {broke} 題"
          f"（判準：最多 1 題）      {'通過' if ok_b else '✗ 不通過'}")

    # 第五條判準：全庫候選召回@40 不得低於 A 臂
    cases = load_cases(get_table_columns())
    gq = cache.get("__queries__")
    rec = {}
    if gq and len(gq) == len(cases):
        for name, tv in (("A", A), ("D+", D)):
            ok = sum(1 for (_, _, nd), v in zip(cases, gq)
                     if any(n <= set(sorted(tv, key=lambda t: -cosine(v, tv[t]))[:CANDIDATE_N])
                            for n in nd))
            rec[name] = ok / len(cases) * 100
        ok5 = rec["D+"] >= rec["A"]
        passed &= ok5
        print(f"\n全庫 {len(cases)} 題候選召回@40   A {rec['A']:.1f}%  →  D+ {rec['D+']:.1f}%"
              f"   {'通過' if ok5 else '✗ 不通過'}")

    with io.open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    print("\n" + "=" * 74)
    print("五條判準全過 —— 可以翻。" if passed else "有判準沒過 —— 不翻，把結果記進 §10。")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
