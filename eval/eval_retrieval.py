"""
檢索層評估 —— 選對表了嗎？
====================================================================
這支程式不跑 pipeline、不呼叫生成模型，只量檢索本身。理由：檢索錯了，
後面再怎麼修 Prompt 都沒用；而把它跟端到端正確率混在一起量，就分不出
「表沒給對」和「表給對了但 SQL 寫錯」。

Ground Truth 來自 eval_schema_need.py：用 sqlglot 剖析每題的 GT SQL，
得到「這題非有不可的表」。指標有三個，缺一不可：

  召回率  required ⊆ retrieved 的題數比例。這是硬指標 —— 少一張表，
          那題必然答不出來（或更糟：模型用別的表湊一個看起來合理的答案）。
  表數    平均帶進 Prompt 幾張表。這是省下來的成本，愈低愈好。
  淨度    retrieved 裡有多少比例是真的需要的。

召回率與表數是對立的：K 開大召回必然上升、表數也上升。所以兩個要一起看，
單看任何一個都會做出錯誤的取捨。

用法：
    python eval/eval_retrieval.py                  # 純相似度 K=3（預設）
    python eval/eval_retrieval.py --k 3 4 6        # 掃多個 K
    python eval/eval_retrieval.py --funnel         # 量現行的三段漏斗（會打 LLM）

--funnel 是現行 production 路徑，其餘設定量的是「不用 LLM 選表」的基準。
兩者一起跑就能回答「這一層值不值得那一次 LLM 呼叫」——
最近一次的答案是值得：91.4% / 4.9 張表 → 99.3% / 2.1 張表。
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
from collections import Counter

import yaml
from loguru import logger as log

from eval_schema_need import required_schema
from langgraph_sql.utils.schema_graph import find_join_path
from langgraph_sql.utils.schema_registry import get_table_columns
from langgraph_sql.utils.table_retriever import (
    _cosine, _embed, get_table_vectors, pick_anchors,
)

GT_PATH = _os.path.join(_ROOT, "eval_ground_truth.yaml")


def load_cases() -> list[tuple[int, str, list[set[str]]]]:
    """回傳 [(題號, 問題, [需要的表, ...])]。防禦題沒有 GT SQL，排除。

    **每題回傳的是一串候選需求集，不是一個。** 題目本身語意有歧義時 GT 會列
    `alt_sql`（任一相符即通過，`eval_score.judge()` 就是這樣判的）——
    那些替代寫法**用的表不一樣**，例如 `#172`「有多少張訂單曾經被取消過」
    走 `order_status_history` 或 `order_cancellations` 都對。

    這支程式原本只解 `entry["sql"]`，於是把「走了另一條被 GT 認可的路」
    判成檢索失敗。實測 257 題裡 36 題有 alt_sql，而某一輪 `--funnel` 漏掉的
    6 題裡有 4 題是這樣誤報的 —— **檢索召回被系統性低報。**

    這是「對照組與檢查工具本身也要驗」的又一個實例（ARCHITECTURE.md §8 ④）：
    兩支評估程式對「答對」的定義不一致，而不一致的那一支不會報錯。
    """
    known = get_table_columns()
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    cases = []
    for entry in gt:
        if entry["expect"] == "schema_unsupported" or not entry.get("sql"):
            continue
        needs: list[set[str]] = []
        for sql in [entry["sql"]] + list(entry.get("alt_sql") or []):
            try:
                need, _ = required_schema(sql, known)
            except Exception:
                continue
            if need and need not in needs:
                needs.append(need)
        if needs:
            cases.append((entry["id"], entry["question"], needs))
    return cases


def run_filter(questions: list[str], rankings: list[list], workers: int) -> list[list[str]]:
    """
    每題跑一次 LLM 選表。回傳 [[表名]]，某題失敗就是空陣列。

    並行是為了讓 139 題在幾分鐘內跑完，但 NIM 在 6 條並行時會回 503 ——
    實測 139 題有 13 題拿到空回覆而靜默降級，把召回從 98.6% 拉到看起來像
    97.8%。並行數要保守，而且失敗題數一定要回報給呼叫端。
    """
    from concurrent.futures import ThreadPoolExecutor

    from langgraph_sql.utils.table_filter import filter_tables, get_candidate_n

    top_n = get_candidate_n()

    def one(pair):
        question, ranking = pair
        return filter_tables(question, [t for t, _ in ranking[:top_n]])

    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(one, zip(questions, rankings)))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, nargs="+", default=[3], help="要測的 top-K")
    ap.add_argument("--ratio", type=float, nargs="+", default=[],
                    help="改測動態門檻：保留分數 >= 第一名 x ratio 的表")
    ap.add_argument("--max-k", type=int, default=6, help="動態門檻的錨點數上限")
    ap.add_argument("--funnel", action="store_true",
                    help="改量三段漏斗（LLM 選表 ∪ 相似度第 1 名），會打 LLM")
    ap.add_argument("--workers", type=int, default=2,
                    help="--funnel 的並行數。實測 6 條並行會踩到 NIM 503")
    ap.add_argument("--verbose", action="store_true", help="列出每一題漏掉的表")
    args = ap.parse_args()

    cases = load_cases()
    log.remove()
    print(f"可評估題數: {len(cases)}\n")

    vectors = get_table_vectors()
    total_tables = len(vectors)
    qvecs = _embed([q for _, q, _ in cases], "query")

    # 每題對所有表排序一次（含分數），各種設定共用，不必重算
    rankings = [
        sorted(((t, _cosine(qv, v)) for t, v in vectors.items()),
               key=lambda x: (-x[1], x[0]))
        for qv in qvecs
    ]

    # (標籤, 取錨點的函式)。函式簽名是 (第 i 題, 該題的排名) → 錨點清單
    configs: list[tuple[str, object]] = [
        (f"K={k}", (lambda i, r, k=k: [t for t, _ in r[:k]])) for k in args.k
    ] + [
        (f"r={ratio}", (lambda i, r, ratio=ratio: pick_anchors(
            r, ratio=ratio, max_k=args.max_k))) for ratio in args.ratio
    ]

    if args.funnel:
        # LLM 呼叫先一次跑完（可並行），評分迴圈才不用邊算邊等網路
        picks = run_filter([q for _, q, _ in cases], rankings, args.workers)
        fell_back = sum(1 for p in picks if not p)
        if fell_back:
            # 靜默降級是這一層最危險的失敗模式，一定要印出來，
            # 否則會把「LLM 沒跑」的結果當成「LLM 的成績」
            print(f"⚠️  {fell_back} 題的 LLM 選表失敗、已退回相似度 —— "
                  f"下面的數字被稀釋了，降 --workers 再跑一次\n")

        def funnel(i, ranking, picks=picks):
            if not picks[i]:
                return [t for t, _ in ranking[:4]]
            return list(dict.fromkeys(picks[i] + [ranking[0][0]]))

        # 對照臂：LLM 選表但**不** ∪ 相似度第 1 名。
        # ∪Top-1 是 §2.4 那道保險，而它的價值一直只有 21 張表年代的數字。
        # 兩臂共用同一組 picks —— 這個對照因此是**零額外 LLM 呼叫**的，
        # 沒有理由不順便量。降級路徑（LLM 失敗退回 top-4）保持一致，
        # 否則比較到的會是「誰比較常降級」而不是「∪Top-1 值多少」。
        def llm_only(i, ranking, picks=picks):
            if not picks[i]:
                return [t for t, _ in ranking[:4]]
            return list(dict.fromkeys(picks[i]))

        configs.insert(0, ("漏斗-無∪T1", llm_only))
        configs.insert(0, ("漏斗", funnel))

        # 把 LLM 的選擇存檔。--funnel 是本專案唯一要花 253 次 LLM 呼叫的檢索評估，
        # 而「漏了哪幾題、漏在哪一層」是事後才會想問的問題 —— 沒存檔就得整輪重跑。
        # 存了之後所有後續分析（層級歸因、跟下一輪比對）都是零成本的。
        import json as _json
        import time as _time
        _os.makedirs(_RESULTS, exist_ok=True)
        _pick_path = _os.path.join(_RESULTS, f"funnel_picks_{int(_time.time())}.json")
        with io.open(_pick_path, "w", encoding="utf-8") as fh:
            _json.dump([{"id": qid, "question": q,
                         "needs": [sorted(nd) for nd in needs],
                         "llm_pick": picks[i], "top1": rankings[i][0][0],
                         "rank_of_need": {t: [x for x, _ in rankings[i]].index(t) + 1
                                          for t in sorted(set().union(*needs))
                                          if t in dict(rankings[i])}}
                        for i, (qid, q, needs) in enumerate(cases)],
                       fh, ensure_ascii=False, indent=1)
        print(f"LLM 選表結果已存檔: {_pick_path}\n")

    print(f"{'設定':>7}  {'錨點召回':>8}  {'+KMB 召回':>9}  {'平均表數':>8}  "
          f"{'佔全庫':>7}  {'淨度':>6}  {'平均錨點':>8}")
    print("-" * 70)

    results = {}
    for label, pick in configs:
        anchor_hit = kmb_hit = 0
        sizes: list[int] = []
        purity: list[float] = []
        n_anchors: list[int] = []
        misses: list[tuple[int, str, set[str], set[str]]] = []

        for i, ((qid, question, needs), ranking) in enumerate(zip(cases, rankings)):
            anchors = pick(i, ranking)
            n_anchors.append(len(anchors))
            if any(nd <= set(anchors) for nd in needs):
                anchor_hit += 1
            tables = find_join_path(anchors)
            sizes.append(len(tables))
            # 淨度用「跟實際帶進來的表重疊最多」的那一組需求算 —— 題目有多條
            # 合法解時，拿沒被走的那一條當分母會低估淨度。
            ref = max(needs, key=lambda nd: len(nd & tables))
            purity.append(len(ref & tables) / len(tables))
            if any(nd <= tables for nd in needs):
                kmb_hit += 1
            else:
                # 漏了什麼：報「最接近的那一條解」還缺哪幾張，
                # 而不是主 SQL 那條 —— 前者才是模型差一步就到的路。
                misses.append((qid, question, ref - tables, tables))

        n = len(cases)
        avg = sum(sizes) / n
        print(f"{label:>7}  {anchor_hit / n:>7.1%}  {kmb_hit / n:>8.1%}  "
              f"{avg:>8.1f}  {avg / total_tables:>6.1%}  "
              f"{sum(purity) / n:>5.1%}  {sum(n_anchors) / n:>8.1f}")
        results[label] = misses

    for label, _ in configs:
        misses = results[label]
        if not misses:
            continue
        # 題號一律印（漏掉的題通常個位數，而沒有題號就沒辦法歸因、
        # 也沒辦法跟下一輪比對是哪幾題變了）。--verbose 才印問題與給了什麼。
        print(f"\n{label} 漏掉的 {len(misses)} 題: "
              f"{[q for q, _, _, _ in misses]}")
        lost = Counter()
        for qid, question, missing, got in misses:
            lost.update(missing)
            if args.verbose:
                print(f"  #{qid:<4} 缺 {sorted(missing)}")
                print(f"        問題: {question[:50]}")
                print(f"        給了: {sorted(got)}")
        print(f"  最常漏掉的表: {dict(lost.most_common(8))}")

    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
