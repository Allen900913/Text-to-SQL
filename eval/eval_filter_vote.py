"""方案 B：打散候選順序、多次取樣、取聯集（MCS-SQL 的做法）

為什麼是這一招（ARCHITECTURE.md §7.9）：
  41 張表這一輪，最終 prompt 漏掉的表相似度排名是 2 / 2 / 4 / 10 ——
  **相似度給了，LLM 沒選**。而且 `#154` 在 n=8 的重複取樣裡是 4/8：
  它不是「模型不會」，是**擲硬幣**。

  單次呼叫召回 50% 的題，取 3 次聯集就是 1 − 0.5³ = 87.5%。
  這是純機率，不需要模型變聰明 —— 這才是 B 的機制，不是「修正位置偏誤」。
  （位置偏誤在這裡不是主因：漏掉的表排名 2 和 4，都在清單最前面。）

  MCS-SQL 用聯集而不是投票多數決的理由，跟本專案 §2.6 選 ∪Top-1 的理由
  逐字相同：「多選幾張表對後續 SQL 生成影響不大，漏掉必要的表則會讓
  查詢根本無法正確生成」。所以 B 是既有設計原則的延伸，不是新原則。

效率：跑 N 次取樣**一次就得到整條曲線**。
  第 i 次的結果單獨看 = N=1 的一個估計（而且有 N 個估計，可以看變異）
  前 2 次的聯集     = N=2
  全部 N 次的聯集   = N=3
所以 155 題 × 3 次 = 465 次呼叫，換到的是 N=1/2/3 三個設定的完整對照，
外加 N=1 的三次重複 —— 後者很重要，否則分不出「B 有效」與「這輪運氣好」。

用法：
    python eval/eval_filter_vote.py            # N=3
    python eval/eval_filter_vote.py --votes 4 --workers 2
"""
import os as _os
import sys as _sys

# 搬進子目錄之後要自己把專案根目錄放進 sys.path（tools/ 也是這個寫法）。
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_RESULTS = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "results")

import argparse
import io
import sys
from concurrent.futures import ThreadPoolExecutor

import yaml
from loguru import logger as log

from eval_schema_need import required_schema
from langgraph_sql.utils.schema_graph import find_join_path
from langgraph_sql.utils.schema_registry import get_table_columns
from langgraph_sql.utils.table_filter import filter_tables, get_candidate_n
from langgraph_sql.utils.table_retriever import (
    _cosine, _embed, get_table_vectors,
)

GT_PATH = _os.path.join(_ROOT, "eval_ground_truth.yaml")


def load_cases(known):
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    out = []
    for e in gt:
        if e.get("expect") == "schema_unsupported" or not e.get("sql"):
            continue
        try:
            need, _ = required_schema(e["sql"], known)
        except Exception:
            continue
        if need:
            out.append((e["id"], e["question"], need))
    return out


def bridge(tables: list[str]) -> list[str]:
    """補上 KMB 橋接表，跟 production 走同一條路徑。"""
    if len(tables) < 2:
        return list(tables)
    try:
        return list(find_join_path(tables) or tables)
    except Exception:
        return list(tables)


def score(name: str, picks: list[set[str]], cases, distractors) -> dict:
    ok = sum(1 for (_, _, need), p in zip(cases, picks) if need <= p)
    okb = sum(1 for (_, _, need), p in zip(cases, picks)
              if need <= set(bridge(sorted(p))))
    sizes = [len(p) for p in picks]
    pure = [len(need & p) / len(p) for (_, _, need), p in zip(cases, picks) if p]
    dirty = sum(1 for p in picks if p & distractors)
    return {"name": name,
            "recall": ok / len(cases) * 100,
            "recall_kmb": okb / len(cases) * 100,
            "tables": sum(sizes) / len(sizes),
            "purity": sum(pure) / len(pure) * 100 if pure else 0.0,
            "dirty": dirty}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--votes", type=int, default=3)
    ap.add_argument("--fixed-first", action="store_true", default=True,
                    help="第 0 次用固定順序當對照（預設開）")
    ap.add_argument("--all-shuffled", dest="fixed_first", action="store_false")
    ap.add_argument("--workers", type=int, default=2,
                    help="實測 6 條並行會踩到 NIM 503，並行數要保守")
    args = ap.parse_args()

    known = get_table_columns()
    cases = load_cases(known)
    from langgraph_sql.utils.schema_registry import get_schema_parser
    distractors = set(known) - {k.lower() for k in
                                get_schema_parser().get_table_columns()}
    log.remove()

    vectors = get_table_vectors()
    qvecs = _embed([q for _, q, _ in cases], "query")
    top_n = get_candidate_n()
    rankings = [[t for t, _ in sorted(((t, _cosine(qv, v)) for t, v in vectors.items()),
                                      key=lambda p: (-p[1], p[0]))]
                for qv in qvecs]
    cands = [r[:top_n] for r in rankings]
    top1 = [r[0] for r in rankings]

    print(f"題數 {len(cases)}；候選 {top_n} 張；取樣 {args.votes} 次 "
          f"= {len(cases) * args.votes} 次 LLM 呼叫\n")

    # (題index, 第幾次取樣) → 選出的表
    jobs = [(i, v) for v in range(args.votes) for i in range(len(cases))]

    def one(job):
        i, v = job
        # 第 0 次刻意用**固定順序**（production 現況），其餘打散。
        # 沒有這個對照就分不出「聯集有效」與「光是打散順序就有效」——
        # 那是兩個完全不同的結論，一個要多花 N 倍呼叫，一個免費。
        # seed 綁 (題, 次數)：同一題每次順序不同，但整份實驗可重現
        seed = None if (v == 0 and args.fixed_first) else 1000 * v + i
        return filter_tables(cases[i][1], cands[i], shuffle_seed=seed)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(one, jobs))

    picks: dict[tuple[int, int], set[str]] = {}
    fallbacks = 0
    for (i, v), r in zip(jobs, results):
        if not r:
            fallbacks += 1
        picks[(i, v)] = set(r)

    if fallbacks:
        print(f"⚠️  {fallbacks}/{len(jobs)} 次呼叫回空（LLM 失敗，靜默退回相似度）。"
              f"跨變體比較必須記錄這個數字，否則量到的是退化的路徑不是變體本身。\n")

    rows = []
    # N=1 的三個獨立估計 —— 沒有這幾行就分不出「B 有效」與「這輪運氣好」
    for v in range(args.votes):
        tag = "固定順序" if (v == 0 and args.fixed_first) else f"打散 #{v}"
        rows.append(score(f"N=1（{tag}）",
                          [picks[(i, v)] | {top1[i]} for i in range(len(cases))],
                          cases, distractors))
    for n in range(2, args.votes + 1):
        rows.append(score(f"N={n}（前 {n} 次聯集）",
                          [set().union(*(picks[(i, v)] for v in range(n))) | {top1[i]}
                           for i in range(len(cases))],
                          cases, distractors))

    head = (f"{'設定':<26}{'錨點召回':>9}{'+KMB':>8}{'平均表數':>9}"
            f"{'淨度':>8}{'含干擾題數':>11}")
    print(head)
    print("-" * len(head))
    for r in rows:
        print(f"{r['name']:<26}{r['recall']:8.1f}%{r['recall_kmb']:7.1f}%"
              f"{r['tables']:9.2f}{r['purity']:7.1f}%{r['dirty']:11d}")
    print("\n註：全部設定都已 ∪ 相似度 Top-1（§2.6 的既有機制），"
          "所以這裡量的是「多取樣」帶來的**額外**增益。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
