# -*- coding: utf-8 -*-
"""某個句型從檢索文件裡拿掉之後，**哪些題會動** —— 普查，不是抽樣。

為什麼是普查
================================================================
嵌入是確定性的，投影只改少數幾份文件（`-enum` 只動 orders / payments 兩張）。
所以「哪些題會動」是可以窮舉的事實，不需要出保留組去估。先窮舉，再決定
還需不需要檢定：如果變動題全部落在某個題庫形狀上（例如問句自己寫了英文
代碼），那就不是效果量的問題，是題庫量錯了東西（memory:
`stable-failures-are-usually-the-benchmark`）。

硬幣池要先扣掉
================================================================
嵌入是確定性的，但「確定性」不等於「穩定」。全庫 305 題的第一二名餘弦差距
中位數是 0.047，而窄表與它的 `_profiles` 寬表在通用問句上常常只差 0.0005 ——
差了兩個數量級。落在那個區間的題，**任何**對兩張文件之一的編輯都會翻它，
翻的方向與編輯內容無關。把它們算進總分，等於用擲硬幣的結果評判修改
（memory: `n3-per-question-is-not-evidence` 的確定性版本）。

所以 [3] 只在差距 >= 全庫 p25 的題上計分。硬幣題照樣列出來 —— 它們不是
待辦事項，是**這一對表在向量層根本分不開**的證據，該由候選目錄那層處理。

輸出四段：
    [1] 血緣    這個投影改了哪幾張表、各少幾個字元
    [2] 逐題    A 與 X 臂名次不同的**全部**題目，標出 GT 表與搶第一的表
    [3] 計分    扣掉硬幣池之後的修好／弄壞，再按窄表 GT／寬表 GT 切開
    [4] 分群    變動題按「問句是否含英文代碼」切開

用法：python tools/probes/kind_diff.py enum
"""
import io
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "eval"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

from loguru import logger as log  # noqa: E402

from brief_arms import CACHE, load_cases, vectors  # noqa: E402
from langgraph_sql.utils.embedding import cosine, embed  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.table_filter import get_table_briefs  # noqa: E402
from langgraph_sql.utils.table_semantics import KINDS, compose, load  # noqa: E402

# 問句裡的英文代碼：連續大寫（含底線）且長度 >= 3。用來把「題庫用代碼提問」
# 這個形狀跟「中文語意題」分開 —— 兩者對 enum 句的需求是相反的。
_CODE = re.compile(r"\b[A-Z][A-Z_]{2,}\b")


def rank_list(tv, v):
    return [t for t, _ in sorted(((t, cosine(v, x)) for t, x in tv.items()),
                                 key=lambda p: (-p[1], p[0]))]


def main(kind: str) -> int:
    keep = tuple(k for k in KINDS if k != kind)
    d = load()
    base = get_table_briefs()
    arm = {t: compose(cl, keep) for t, cl in d.items()}

    changed = sorted((t for t in base if base[t] != arm[t]),
                     key=lambda t: len(arm[t]) - len(base[t]))
    print(f"[1] 血緣　-{kind} 改寫 {len(changed)}/{len(base)} 張表")
    for t in changed:
        gone = [c["text"] for c in d[t] if c["kind"] == kind]
        print(f"      {t:<24}{len(arm[t]) - len(base[t]):>5} 字元   "
              f"{' ｜ '.join(gone)[:56]}")

    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    A, X = vectors(base, cache), vectors(arm, cache)
    cases = load_cases(get_table_columns())
    qv = cache.get("__queries__")
    if not qv or len(qv) != len(cases):
        print(f"    嵌入 {len(cases)} 個問句…")
        qv = cache["__queries__"] = embed([q for _, q, _ in cases], "query")
    with io.open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)

    # 硬幣線：A 臂第一二名的餘弦差距，取全庫 p25。差距比這窄的題，
    # 誰第一是這一對表的相對長度決定的，不是問句決定的。
    gaps = []
    for v in qv:
        sc = sorted((cosine(v, x) for x in A.values()), reverse=True)
        gaps.append(sc[0] - sc[1])
    coin = sorted(gaps)[len(gaps) // 4]

    rows = []
    for (qid, q, needs), v, g in zip(cases, qv, gaps):
        ra, rx = rank_list(A, v), rank_list(X, v)
        oa = any(ra[0] in n for n in needs)
        ox = any(rx[0] in n for n in needs)
        if ra[0] != rx[0] or oa != ox:
            rows.append((qid, q, needs, ra[0], rx[0], oa, ox, g))

    ta = sum(1 for (_, _, nd), v in zip(cases, qv) if any(rank_list(A, v)[0] in n for n in nd))
    tx = sum(1 for (_, _, nd), v in zip(cases, qv) if any(rank_list(X, v)[0] in n for n in nd))
    print(f"\n[2] 逐題　Top-1 {ta}/{len(cases)} ({ta / len(cases) * 100:.1f}%)"
          f" → {tx}/{len(cases)} ({tx / len(cases) * 100:.1f}%)；"
          f"第一名改變的題 {len(rows)} 題\n")
    print(f"    硬幣線 = 全庫第一二名餘弦差距的 p25 = {coin:.4f}"
          f"（中位 {sorted(gaps)[len(gaps) // 2]:.4f}）")
    print()
    print(f"{'題':>5} {'碼':^3}{'幣':^3}{'判':^7}{'A 搶第一':<22}{'X 搶第一':<22}問句")
    print("-" * 112)
    for qid, q, needs, a1, x1, oa, ox, g in rows:
        code = "英" if _CODE.search(q) else ""
        verdict = "修好" if (ox and not oa) else ("弄壞" if (oa and not ox) else "平手")
        print(f"#{qid:<4d} {code:^3}{'幣' if g < coin else '':^3}{verdict:^6}"
              f"{a1:<22}{x1:<22}{q[:28]}")

    wide = {t for t in base if t.endswith("_profiles")}
    real = [r for r in rows if r[7] >= coin]
    print()
    print(f"[3] 計分　扣掉 {len(rows) - len(real)} 題硬幣，剩 {len(real)} 題可計分")
    for lab, sel in (("窄表 GT", False), ("寬表 GT", True)):
        sub = [r for r in real if any(t in wide for n in r[2] for t in n) == sel]
        fx = [r[0] for r in sub if r[6] and not r[5]]
        bk = [r[0] for r in sub if r[5] and not r[6]]
        print(f"      {lab}   修好 {len(fx)} {fx}　弄壞 {len(bk)} {bk}")
    fx = sum(1 for r in real if r[6] and not r[5])
    bk = sum(1 for r in real if r[5] and not r[6])
    print(f"      可計分淨值 {fx - bk:+d}　"
          f"（含硬幣的總分淨值 {tx - ta:+d} —— 差額就是硬幣貢獻的）")

    print(f"\n[4] 分群　（『碼』＝問句自己寫了英文代碼，是題庫形狀不是模型能力）")
    for tag, sel in (("問句含英文代碼", lambda q: _CODE.search(q)),
                     ("純中文問句", lambda q: not _CODE.search(q))):
        sub = [r for r in rows if sel(r[1])]
        fix = sum(1 for r in sub if r[6] and not r[5])
        brk = sum(1 for r in sub if r[5] and not r[6])
        print(f"      {tag:<16}變動 {len(sub):2d} 題　修好 {fix:2d}　弄壞 {brk:2d}　"
              f"淨 {fix - brk:+d}")
    allcode = sum(1 for _, q, _ in cases if _CODE.search(q))
    print(f"      題庫裡含英文代碼的題共 {allcode}/{len(cases)} 題")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "enum"))
