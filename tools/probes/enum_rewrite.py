# -*- coding: utf-8 -*-
"""enum 句：刪掉 vs 換成中文核心業務說明 —— 三臂，確定性，零 LLM

為什麼要第三臂
================================================================
`kind_diff.py enum` 量到 `-enum` 在 305 題上淨 -3，而退步的 4 題**全是窄表
GT**（orders 掉下去輸給 order_profiles）。同一支程式量 `-ptr` 得到鏡像的
結果（窄表 GT 修好 12 弄壞 1、寬表 GT 修好 1 弄壞 6）。兩個投影落在**同一條
窄↔寬軸**上，正負只由題庫的 8:2 配比決定 —— 那是門檻位移，不是鑑別力。

所以「刪掉 enum」這個臂測不到原則本身：它把「移除英文代碼」與「移除 48 個
字元的長度質量」綁在一起。第三臂把長度還回去、只換掉內容：

    A        訂單狀態 PENDING/PAID/SHIPPED/COMPLETED/CANCELLED ——
    -enum    （整句拿掉）
    中文      訂單狀態沿付款與出貨進度單向推進，取消之後不再前進 ——

改寫的內容刻意**不重複相鄰 usage 句的值列表**（那裡已經有「待處理、已付款、
已出貨、已完成、已取消」），寫的是值域背後的業務規則。這是「欄位資訊要進
表註解就必須是核心業務」那條原則的直接實作。

可證偽的預測（跑之前寫下）：
    若英文代碼是純長度質量 → 中文臂上 #8 #38 #75 回來（窄表 GT 不掉）
    若代碼有語意作用        → #50（問句含 COMPLETED/CANCELLED）在中文臂仍掉
    兩者都不成立            → 原則在檢索層沒有可測的效果，別動

91/93 張文件不變 → 其餘題目是免費的雜訊地板（單層介入自帶對照組）。
"""
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "eval"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

from loguru import logger as log  # noqa: E402

from brief_arms import CACHE, load_cases, vectors  # noqa: E402
from langgraph_sql.utils.embedding import cosine, embed  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.table_filter import CANDIDATE_N, get_table_briefs  # noqa: E402
from langgraph_sql.utils.table_semantics import KINDS, compose, load  # noqa: E402

NOE = tuple(k for k in KINDS if k != "enum")

# 換文：值域背後的業務規則，不是值列表。兩句都通得過「核心業務」這關 ——
# 「取消之後不再前進」與「同一張訂單可能有多筆付款」都是寫 SQL 會用到的事實。
REWRITE = {
    "orders":   "訂單狀態沿付款與出貨進度單向推進，取消之後不再前進",
    "payments": "付款可能失敗或事後退款，同一張訂單因此可能有多筆付款紀錄",
}


def arm_rewrite(d):
    out = {}
    for t, cl in d.items():
        if t in REWRITE:
            cl = [dict(c, text=REWRITE[t]) if c["kind"] == "enum" else c for c in cl]
        out[t] = compose(cl, KINDS)
    return out


def top1(tv, v):
    return min(tv, key=lambda t: (-cosine(v, tv[t]), t))


def main() -> int:
    d = load()
    arms = {"A 現行": get_table_briefs(),
            "-enum 刪": {t: compose(cl, NOE) for t, cl in d.items()},
            "中文 換": arm_rewrite(d)}
    print("三臂的 orders / payments 文件長度")
    for n, b in arms.items():
        print(f"  {n:<10}orders {len(b['orders']):>4}　payments {len(b['payments']):>4}"
              f"　（order_profiles {len(b['order_profiles'])}）")

    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    tv = {n: vectors(b, cache) for n, b in arms.items()}
    cases = load_cases(get_table_columns())
    qv = cache.get("__queries__")
    if not qv or len(qv) != len(cases):
        qv = cache["__queries__"] = embed([q for _, q, _ in cases], "query")
    with io.open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)

    names = list(arms)
    hit = {n: 0 for n in names}
    rec = {n: 0 for n in names}
    moved = []
    for (qid, q, needs), v in zip(cases, qv):
        t1 = {n: top1(tv[n], v) for n in names}
        for n in names:
            hit[n] += any(t1[n] in nd for nd in needs)
            r = sorted(tv[n], key=lambda t: -cosine(v, tv[n][t]))[:CANDIDATE_N]
            rec[n] += any(nd <= set(r) for nd in needs)
        if len(set(t1.values())) > 1:
            moved.append((qid, q, needs, t1))

    N = len(cases)
    print(f"\n{'臂':<10}{'Top-1':>10}{'候選@40':>11}")
    print("-" * 32)
    for n in names:
        print(f"{n:<10}{hit[n]:>4}/{N} {hit[n] / N * 100:5.1f}%{rec[n] / N * 100:9.1f}%")

    print(f"\n第一名在三臂之間有差異的題：{len(moved)} 題（其餘 {N - len(moved)} 題是雜訊地板）\n")
    print(f"{'題':>5}  " + "".join(f"{n:<22}" for n in names) + "問句")
    print("-" * 122)
    for qid, q, needs, t1 in moved:
        cells = ""
        for n in names:
            ok = "○" if any(t1[n] in nd for nd in needs) else "✗"
            cells += f"{ok}{t1[n]:<21}"
        print(f"#{qid:<4d} {cells}{q[:26]}")

    print("\n對事前預測：")
    look = {q: (n, t) for q, _, _, t in
            ((m[0], m[1], m[2], m[3]) for m in moved) for n, t in [(None, None)]}
    tab = {m[0]: m for m in moved}
    for qid, why in ((8, "純中文窄表題"), (38, "純中文窄表題"), (75, "純中文窄表題"),
                     (50, "問句含 COMPLETED/CANCELLED"), (291, "寬表→窄表")):
        if qid not in tab:
            print(f"  #{qid:<4d}{why:<26}三臂相同（未變動）")
            continue
        _, _, needs, t1 = tab[qid]
        s = "　".join(f"{n} {'○' if any(t1[n] in nd for nd in needs) else '✗'}" for n in names)
        print(f"  #{qid:<4d}{why:<26}{s}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
