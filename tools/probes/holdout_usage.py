# -*- coding: utf-8 -*-
"""用途句（usage）的保留組 —— 零 LLM，確定性

兩段跑法，中間停下來登記判準（criteria-need-headroom 的教訓）：

    python tools/probes/holdout_usage.py --baseline    # 只印 A 臂基線
    python tools/probes/holdout_usage.py               # 兩臂比較 + 對判準

判準寫在 CRITERIA 裡，是看完基線之後填的 —— 量基線不含處理組資訊，
嵌入又是確定性的，沒有回歸均值的問題。
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

from brief_arms import CACHE, arm_no_usage, load_cases, vectors  # noqa: E402
from langgraph_sql.utils.embedding import cosine, embed  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.table_filter import CANDIDATE_N, get_table_briefs  # noqa: E402

SPEC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holdout_usage.yaml")

# ── 事前登記的判準（2026-09-09，看完 A 臂基線、未看 U− 臂之後填）────────
#
# 基線：U1 5/9（均名 2.0）、U2 4/5（1.2）、U3 4/5（1.4）——三組都有餘裕。
#
# 要證偽的虛無假設：**用途句在新問句上沒有作用，305 題上的效果是貼題的。**
# 所以判準的方向是反的 —— 拿掉用途句要讓 U1「掉」，掉了才代表它有遷移。
#
#   U1  掉 >= 2 題（5/9 → <= 3/9）    用途句的目標形狀（商業動詞）；不掉就是沒作用
#   U2  掉的題數 < U1 掉的題數         機制必須是「動詞 vs 名詞落差」，
#                                     若名詞題掉得一樣多，那只是普遍變差
#   U3  掉 <= 1 題                    誘餌端；usage 表是 GT 最熱的幾張，
#                                     拿掉之後不該讓窄表題失控
#
# 三條全過 → 用途句有遷移，留著，305 題的結果可信
# U1 不過   → 用途句不遷移，是貼題的，跟 D+ 同一個結局，進 §10
CRITERIA = {
    "U1": ("掉 >= 2 題（證明有遷移）", lambda a, u, n: a - u >= 2),
    "U2": ("掉的題數要少於 U1", None),          # 跨組，在 main 裡算
    "U3": ("掉 <= 1 題（誘餌端）", lambda a, u, n: a - u <= 1),
}


def rank_of(tv, v, want):
    return sorted(tv, key=lambda t: -cosine(v, tv[t])).index(want) + 1


def main() -> int:
    spec = yaml.safe_load(io.open(SPEC, encoding="utf-8"))["groups"]
    base = get_table_briefs()
    noU = arm_no_usage(base)
    changed = [t for t in base if base[t] != noU[t]]
    print(f"U− 臂改寫 {len(changed)} 張表：{sorted(changed)}\n")

    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    A, U = vectors(base, cache), vectors(noU, cache)
    qs = [(g, i["q"], i["want"]) for g in sorted(spec) for i in spec[g]]
    key = "__holdout_usage__"
    if cache.get(key + "_n") != len(qs):
        print(f"嵌入 {len(qs)} 個保留組問句…")
        cache[key] = embed([q for _, q, _ in qs], "query")
        cache[key + "_n"] = len(qs)
    qv = cache[key]

    baseline_only = "--baseline" in sys.argv
    print(f"{'組':<5}{'期望表':<20}{'A 名次':>8}" +
          ("" if baseline_only else f"{'U− 名次':>9}") + "   問句")
    print("-" * (78 if baseline_only else 90))
    tally = {}
    for (g, q, want), v in zip(qs, qv):
        ra = rank_of(A, v, want)
        s = tally.setdefault(g, {"a": 0, "u": 0, "n": 0, "ra": 0, "ru": 0})
        s["n"] += 1
        s["a"] += ra == 1
        s["ra"] += ra
        line = f"{g:<5}{want:<20}{ra:8d}"
        if not baseline_only:
            ru = rank_of(U, v, want)
            s["u"] += ru == 1
            s["ru"] += ru
            mark = "  ←U−變差" if ra == 1 and ru != 1 else ("  ←U−變好" if ru == 1 and ra != 1 else "")
            line += f"{ru:9d}   {q[:26]}{mark}"
        else:
            line += f"   {q[:30]}"
        print(line)

    print(f"\n{'組':<5}{'A Top-1':>9}" + ("" if baseline_only else f"{'U− Top-1':>10}")
          + f"{'A 均名':>8}" + ("" if baseline_only else f"{'U− 均名':>9}"))
    print("-" * (40 if baseline_only else 60))
    for g in sorted(tally):
        s = tally[g]
        line = f"{g:<5}{s['a']:6d}/{s['n']}"
        if not baseline_only:
            line += f"{s['u']:8d}/{s['n']}"
        line += f"{s['ra'] / s['n']:8.1f}"
        if not baseline_only:
            line += f"{s['ru'] / s['n']:9.1f}"
        print(line)

    with io.open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)

    if baseline_only:
        print("\n基線印完。把判準填進 CRITERIA 之後再跑一次（不帶 --baseline）。")
        return 0
    if not CRITERIA:
        print("\n⚠ CRITERIA 還是空的 —— 先登記判準再看結果，不然就是事後編故事。")
        return 1

    print()
    passed = True
    drop = {g: tally[g]["a"] - tally[g]["u"] for g in tally}
    for g, (desc, fn) in CRITERIA.items():
        ok = (drop["U2"] < drop["U1"]) if fn is None else fn(
            tally[g]["a"], tally[g]["u"], tally[g]["n"])
        passed &= ok
        print(f"{g:<5}{desc:<34}掉 {drop[g]:+d} 題   {'通過' if ok else '✗ 不通過'}")

    # 全庫硬指標一起看
    cases = load_cases(get_table_columns())
    gq = cache.get("__queries__")
    if gq and len(gq) == len(cases):
        for name, tv in (("A", A), ("U−", U)):
            ok = sum(1 for (_, _, nd), v in zip(cases, gq)
                     if any(n <= set(sorted(tv, key=lambda t: -cosine(v, tv[t]))[:CANDIDATE_N])
                            for n in nd))
            print(f"    全庫候選召回@40  {name:<3} {ok / len(cases) * 100:.1f}%")

    print("\n" + "=" * 60)
    print("判準全過。" if passed else "有判準沒過。")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
