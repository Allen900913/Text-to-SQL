"""enum 最小集 A/B 的對帳 —— 判準是 ARCHITECTURE.md §9.12 事前登記的那四條。

**這支寫在跑完之前**，所以它不可能是照著結果挑出來的判準。
不要為了讓某一條過而改門檻；要改就先在 §9.12 記下為什麼改。
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

A_PATH = os.path.join(ROOT, "eval", "results", "enum_ab_A.json")
B_PATH = os.path.join(ROOT, "eval", "results", "enum_ab_B.json")

# —— 事前登記的門檻，勿動 ——
MARGIN_CHANGED = -20    # [1] 變動組 B−A 的下限（格）
MARGIN_CONTROL = 10     # [2] 對照組 |B−A| 的上限（格）；超過 → 整批作廢
DEFENCE = [68, 70, 140, 141]

# [4] 機制：R1 的病灶是把「代碼 中文」整串當成值。抓 SQL 裡帶空白 ＋ 中文
# 的字串常值 —— `= 'APP 行動應用'` 是這個形狀，`= '台北'` 不是。
import re  # noqa: E402

_GLUED = re.compile(r"'([A-Za-z][A-Za-z0-9_]*\s+[^']*[一-鿿][^']*)'")


def load(path, name):
    if not os.path.exists(path):
        sys.exit(f"找不到 {name} 的結果檔：{path}")
    d = json.load(open(path, encoding="utf-8"))
    return d, {int(k): v for k, v in d["n_ok"].items()}, \
        {int(k): v for k, v in (d.get("sql") or {}).items()}


def main() -> int:
    dA, A, sqlA = load(A_PATH, "A 臂")
    dB, B, sqlB = load(B_PATH, "B 臂")
    n = dA["n"]

    print("兩臂的指紋（比對的是行程內實際載入的東西，不是環境變數）")
    for tag, d in (("A", dA), ("B", dB)):
        print(f"  {tag}: {d['arm']}")
    assert dA["arm"] != dB["arm"], "兩臂指紋相同 —— 這不是一場 A/B，中止"
    assert dA["n"] == dB["n"], "兩臂的 n 不同，中止"

    changed = set(json.load(open(os.path.join(ROOT, "eval", "enum_ab_changed.json"))))
    control = set(json.load(open(os.path.join(ROOT, "eval", "enum_ab_control.json"))))

    both = sorted(set(A) & set(B))
    missing = (changed | control) - set(both)
    if missing:
        print(f"\n!! 還有 {len(missing)} 題沒跑完（兩臂都要有才算）：{sorted(missing)}")
        print("   下面的數字是**進行中的部分結果**，不可以拿來結案。")

    def tally(group):
        qs = [q for q in both if q in group]
        a, b = sum(A[q] for q in qs), sum(B[q] for q in qs)
        return qs, a, b, len(qs) * n

    qc, ac, bc, tc = tally(changed)
    qk, ak, bk, tk = tally(control)

    def line(title, a, b, t):
        # 進行中的部分結果也要印得出來 —— 跑到一半想看一眼是常態
        print(title)
        if not t:
            print("  （還沒有任何一題兩臂都跑完）")
            return
        print(f"  A {a}/{t} = {a / t:.1%}｜B {b}/{t} = {b / t:.1%}"
              f"｜B−A = {b - a:+d} 格（{(b - a) / t:+.2%}）")

    print(f"\n{'=' * 68}")
    line(f"變動組 {len(qc)} 題 × {n}", ac, bc, tc)
    line(f"對照組 {len(qk)} 題 × {n}（Prompt 位元不變，這裡的差就是雜訊）",
         ak, bk, tk)

    verdicts = []

    ok2 = abs(bk - ak) <= MARGIN_CONTROL
    verdicts.append(("[2] 雜訊地板", ok2,
                     f"對照組 |B−A| = {abs(bk - ak)} ≤ {MARGIN_CONTROL}"))
    ok1 = (bc - ac) >= MARGIN_CHANGED
    verdicts.append(("[1] 非劣性", ok1,
                     f"變動組 B−A = {bc - ac:+d} ≥ {MARGIN_CHANGED}"))

    d_lines = []
    ok3 = True
    for q in DEFENCE:
        if q not in both:
            d_lines.append(f"#{q} 尚未跑完")
            ok3 = False
            continue
        d_lines.append(f"#{q} A {A[q]}/{n} B {B[q]}/{n}")
        if A[q] != n or B[q] != n:
            ok3 = False
    verdicts.append(("[3] 防禦題", ok3, "；".join(d_lines)))

    def glued(sqls):
        hits = []
        for q, lst in sqls.items():
            for s in lst:
                for m in _GLUED.findall(s or ""):
                    hits.append((q, m))
        return hits

    ga, gb = glued(sqlA), glued(sqlB)
    ok4 = len(gb) <= len(ga)
    verdicts.append(("[4] 機制", ok4, f"非法字面值 A {len(ga)} 次、B {len(gb)} 次"))
    for tag, hits in (("A", ga), ("B", gb)):
        for q, m in hits[:8]:
            print(f"    {tag} #{q}: '{m}'")

    print(f"\n{'=' * 68}\n事前登記的四條判準（§9.12）")
    for name, ok, detail in verdicts:
        print(f"  {'過' if ok else '不過'}  {name}：{detail}")

    if missing:
        print("\n進行中，尚未結案。")
        return 0
    if not ok2:
        print("\n判定：**整批作廢**。對照組的 Prompt 位元不變卻差這麼多，"
              "\n      雜訊蓋過效應，[1] 不予採信。")
        return 1
    print("\n判定：" + ("**非劣性成立** —— 可以翻預設值"
                       "（`python tools/gen_enum_fields.py --only utils/enum_minset.txt --write`）。"
                       "\n      能講的只有「排除了大於 4pp 的跌幅」，"
                       "**不准講「有改善」**（§9.12 事前登記）。"
                       if all(v[1] for v in verdicts)
                       else "**否決**，不翻預設值。"))

    print("\n兩個會把結果往上灌水的已知混淆，要單獨看：")
    for q in (159, 104):
        if q in both:
            print(f"  #{q}（expect: empty）A {A[q]}/{n} B {B[q]}/{n}")
    print("  #68 #70 兩題防禦題在變動組裡，它們的 Prompt 會變長。")

    worst = sorted((q for q in qc if B[q] < A[q]), key=lambda q: B[q] - A[q])[:8]
    if worst:
        print("\n變動組裡 B 比 A 差的題（**n=6 的逐題差異不是證據**，"
              "只是給下一步找機制用）：")
        for q in worst:
            print(f"  #{q}  A {A[q]}/{n} → B {B[q]}/{n}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
