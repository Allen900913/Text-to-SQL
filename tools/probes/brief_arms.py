# -*- coding: utf-8 -*-
"""表註解的分欄實驗 —— 三個寬表臂 ＋ 用途句 in/out（零 LLM，確定性）

為什麼要這一支
====================================================================
表註解一段文字身兼三職（DDL / 檢索向量 / LLM 候選目錄），而它裡面其實混了
四種句子：① 粒度＋內容 ② enum 值域 ③ 用途 ④ 邊界與關係。
「③ 用途句對檢索有沒有用」從來沒有被單獨量過 —— `order_items` 那次改動
同時加了內容與用途，兩者綁在一起。

寬表那邊還有一個**已經量錯的東西**。`table_filter._strip_relations()` 按
「。;；——」切句、丟掉含「在 <表名>」的整句，而寬表把「核心業務宣告」與
「關係指路標」寫在同一句裡：

    原  …法規合規與認證。 這是內容與合規面，商品的品名售價在 products，…。
    後  …法規合規與認證                      ← 掉 48 字，「這是內容與合規面」一起沒了

所以 §2.5 記錄的「anchor 變好 14 變差 14、p=0.5747」測的是
「拆掉關係句＋核心業務宣告」，不是「拆掉關係句」。**④ 從沒被乾淨量過。**

這一支把三個臂分開跑，並且**把每一次改寫的前後都印出來**（§8「靜默通過不是
通過」）—— 切錯句是這整件事的原始 bug，不能再靠 regex 沒報錯就當它對了。

臂
====================================================================
    A 現行        概念群 + 核心業務 + 關係              production
    B 只留核心業務  核心業務 + 關係（砍概念群列舉）        使用者提議，從沒測過
    C 只留概念群    概念群（砍核心業務 + 關係）           ≈ _strip_relations
    U- 拿掉用途句   9 張表的 ③ 移除，其餘全同 A          回答「③ 值不值得留」

B / C 只動 14 張 `*_profiles` 裡有「這是」宣告的 13 張。`customer_profiles`
沒有那個句型（結尾是一句用途句），三個臂裡保持不變 —— 它屬於 U- 的受測物。

指標（§5.3：三個一起看，缺一不可）
====================================================================
    候選召回@40   需要的表全部落在前 40 名 —— 硬指標，掉了後面救不回來
    Top-1 命中    ∪Top-1 那道保險吃這個數字
    GT 表平均排名  漏斗的斜率，比通過率早一步看到退化
    寬表搶 Top-1   §2.10 那條雙向軸的誘餌端 —— 對照組一定要含這個
    紅/橙燈表數    沿用閘門 [12]：逐表「被需要時的最差排名」

用法：
    python tools/probes/brief_arms.py            # 四個臂全跑
    python tools/probes/brief_arms.py --arms A B # 只跑指定的
"""
import io
import json
import os
import re
import statistics
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "eval"))

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402

from eval_schema_need import required_schema  # noqa: E402
from langgraph_sql.utils.embedding import cosine as _cosine  # noqa: E402
from langgraph_sql.utils.embedding import doc_hash, embed  # noqa: E402
from langgraph_sql.utils.schema_registry import (  # noqa: E402
    get_schema_parser, get_table_columns,
)
from langgraph_sql.utils.table_filter import CANDIDATE_N, get_table_briefs  # noqa: E402

GT_PATH = os.path.join(_ROOT, "eval_ground_truth.yaml")
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".brief_arms_cache.json")
KS = (3, 5, 10, 20, 40)

# 用途句：「用/看這張表」或「會用到」。刻意**不**收「也在這張表」——
# customer_profiles 中段那句是內容的延續，不是用途（收進來就是切錯句，
# 跟 _strip_relations 犯的是同一個錯）。
_USAGE = re.compile(r"(用|看)這張表|會用到")
_CLAUSE = re.compile(r"[。；;]|——")
_DECL = re.compile(r"。\s*這是")


# ---------------------------------------------------------------- 臂的建構

def arm_wide(briefs: dict, keep: str) -> dict:
    """keep='decl' 只留核心業務+關係；keep='groups' 只留概念群。"""
    out = dict(briefs)
    for t, c in briefs.items():
        if not t.endswith("_profiles"):
            continue
        m = _DECL.search(c)
        if not m:                      # customer_profiles：沒有宣告句，不動
            continue
        head, tail = c[:m.start()], c[m.start() + 1:].lstrip()
        prefix = head.split("：", 1)[0] + "："      # 「商品內容檔案寬表：」
        out[t] = (prefix + tail) if keep == "decl" else head
    return out


_REL_NAME = re.compile(r"在 ([a-z_]{3,})")


def arm_no_pointer(briefs: dict) -> dict:
    """D：概念群 + 核心業務宣告都留，只砍「X 在 <表名>」的指路標。

    為什麼要有這個臂：C 臂修好的 8 題**全部**是「被指路標點名的那張窄表」的題
    （review_profiles→reviews、support_ticket_profiles→support_tickets…），
    8/8 全中。那正是 §2.3 那條硬規則的反面 —— **劃邊界只能用「我是什麼」，
    不能用「我不是什麼」**，而「星等與評價文字在 reviews」就是用指路標寫的
    「我不是什麼」。嵌入不懂否定，它只看到文件裡有 reviews。

    C 臂把宣告句與指路標一起砍掉，於是窄表題修好 8、寬表題弄壞 10。
    這個臂分開它們：留下「我是什麼」，砍掉「我不是什麼」。
    """
    out, known = dict(briefs), set(briefs)
    for t, c in briefs.items():
        if not t.endswith("_profiles"):
            continue
        m = _DECL.search(c)
        if not m:
            continue
        head, tail = c[:m.start()], c[m.start() + 1:].lstrip().rstrip("。")
        kept = [s for s in tail.split("，")
                if not any(w in known for w in _REL_NAME.findall(s))]
        out[t] = head + "。" + "，".join(kept) + "。" if kept else head
    return out


def arm_no_usage(briefs: dict) -> dict:
    out = {}
    for t, c in briefs.items():
        parts = [p.strip() for p in _CLAUSE.split(c) if p.strip()]
        kept = [p for p in parts if not _USAGE.search(p)]
        out[t] = "。".join(kept) if len(kept) != len(parts) else c
    return out


def show_diff(name: str, base: dict, arm: dict) -> None:
    changed = [t for t in base if base[t] != arm[t]]
    print("\n=== 臂 %s：改寫 %d 張表 ===" % (name, len(changed)))
    for t in sorted(changed):
        print("  [%s] %d -> %d 字" % (t, len(base[t]), len(arm[t])))
        print("      前 " + base[t])
        print("      後 " + arm[t])


# ---------------------------------------------------------------- 量測

def load_cases(known):
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    out = []
    for e in gt:
        if e.get("expect") == "schema_unsupported" or not e.get("sql"):
            continue
        needs = []
        for sql in [e["sql"]] + list(e.get("alt_sql") or []):
            try:
                need, _ = required_schema(sql, known)
            except Exception:
                continue
            if need and need not in needs:
                needs.append(need)
        if needs:
            out.append((e["id"], e["question"], needs))
    return out


def vectors(briefs: dict, cache: dict) -> dict:
    docs = {t: "表 %s：%s" % (t, b) for t, b in briefs.items()}
    stale = [d for d in docs.values() if doc_hash(d) not in cache]
    if stale:
        print("    嵌入 %d 份文件…" % len(stale))
        for d, v in zip(stale, embed(stale, "passage")):
            cache[doc_hash(d)] = v
    return {t: cache[doc_hash(d)] for t, d in docs.items()}


def measure(name, briefs, cases, qvecs, distractors, cache) -> dict:
    tvecs = vectors(briefs, cache)
    wide = set(t for t in briefs if t.endswith("_profiles"))
    rankings = []
    for qv in qvecs:
        sc = {t: _cosine(qv, v) for t, v in tvecs.items()}
        rankings.append([t for t, _ in sorted(sc.items(), key=lambda p: (-p[1], p[0]))])

    rec = {}
    for k in KS:
        ok = sum(1 for (_, _, nd), r in zip(cases, rankings)
                 if any(n <= set(r[:k]) for n in nd))
        rec[k] = ok / len(cases) * 100
    top1 = sum(1 for (_, _, nd), r in zip(cases, rankings)
               if any(r[0] in n for n in nd)) / len(cases) * 100
    dist1 = sum(1 for r in rankings if r[0] in distractors)
    wide1 = sum(1 for (_, _, nd), r in zip(cases, rankings)
                if r[0] in wide and not any(r[0] in n for n in nd))

    # GT 表平均排名 ＋ 逐表最差排名（閘門 [12] 的算法）
    pos, per_table = [], {}
    for (_, _, nd), r in zip(cases, rankings):
        idx = dict((t, i + 1) for i, t in enumerate(r))
        best = min(nd, key=lambda n: max(idx.get(t, 999) for t in n))
        for t in best:
            pos.append(idx.get(t, 999))
            per_table.setdefault(t, []).append(idx.get(t, 999))
    red = sum(1 for v in per_table.values() if max(v) > CANDIDATE_N)
    amber = sum(1 for v in per_table.values()
                if max(v) <= CANDIDATE_N and statistics.median(v) > CANDIDATE_N / 2)
    return {"name": name, "rec": rec, "top1": top1, "dist1": dist1, "wide1": wide1,
            "rank": sum(pos) / len(pos), "red": red, "amber": amber,
            "chars": sum(len(b) for b in briefs.values()),
            "per_table": per_table, "rankings": rankings}


def report(rows):
    head = ("%-24s" % "臂") + "".join("@%-5s" % k for k in KS) + \
        "%7s%8s%8s%8s%5s%5s%8s" % ("Top-1", "GT均名", "寬表搶1", "干擾搶1", "紅", "橙", "總字數")
    print("\n" + head)
    print("-" * 100)
    for r in rows:
        print(("%-24s" % r["name"]) + "".join("%5.1f " % r["rec"][k] for k in KS)
              + "%6.1f%%%8.2f%8d%8d%5d%5d%8d" % (
                  r["top1"], r["rank"], r["wide1"], r["dist1"],
                  r["red"], r["amber"], r["chars"]))


def main() -> int:
    want = None
    if "--arms" in sys.argv:
        want = set(sys.argv[sys.argv.index("--arms") + 1:])

    base = get_table_briefs()
    known = get_table_columns()
    cases = load_cases(known)
    yaml_tables = set(k.lower() for k in get_schema_parser().get_table_columns())
    distractors = set(known) - yaml_tables
    print("題數 %d；全庫 %d 張表（干擾表 %d 張）；候選上限 %d"
          % (len(cases), len(known), len(distractors), CANDIDATE_N))

    arms = [
        ("A 現行", base),
        ("B 只留核心業務", arm_wide(base, "decl")),
        ("C 只留概念群", arm_wide(base, "groups")),
        ("D 砍指路標留宣告", arm_no_pointer(base)),
        ("U- 拿掉用途句", arm_no_usage(base)),
    ]
    arms = [(n, b) for n, b in arms if not want or n.split()[0] in want]
    for name, b in arms:
        if b is not base:
            show_diff(name.split()[0], base, b)

    cache = {}
    if os.path.exists(CACHE):
        cache = json.load(io.open(CACHE, encoding="utf-8"))
    qkey = "__queries__"
    if qkey in cache and len(cache[qkey]) == len(cases):
        qvecs = cache[qkey]
    else:
        print("\n嵌入 %d 個問句…" % len(cases))
        qvecs = embed([q for _, q, _ in cases], "query")
        cache[qkey] = qvecs

    rows = []
    for name, b in arms:
        print("\n  量 " + name)
        rows.append(measure(name, b, cases, qvecs, distractors, cache))

    with io.open(CACHE, "w", encoding="utf-8") as f:
        json.dump(cache, f)
    report(rows)

    # 逐表退化清單：總分看不出「哪張表被犧牲了」，而那才是可行動的東西
    if len(rows) > 1:
        a = rows[0]
        for r in rows[1:]:
            worse = []
            for t, v in r["per_table"].items():
                b0, b1 = max(a["per_table"].get(t, [999])), max(v)
                if b1 > b0:
                    worse.append((b1 - b0, t, b0, b1))
            worse.sort(reverse=True)
            print("\n【%s vs A】最差排名退步的表（前 8）：" % r["name"])
            for d, t, b0, b1 in worse[:8]:
                flag = "  ← 掉出候選" if b1 > CANDIDATE_N >= b0 else ""
                print("    %-26s %3d → %3d  (+%d)%s" % (t, b0, b1, d, flag))
            if not worse:
                print("    （沒有表退步）")

    print("\n註：嵌入是確定性的，沒有跑次變異 —— 這張表單次就是結論，不需要重複取樣。")
    print("    紅=最差排名掉出候選的表數；橙=中位數在後半段的表數（閘門 [12] 的判準）。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
