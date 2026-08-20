"""
單題重複取樣 —— 用來判斷「某個修改到底有沒有效」
====================================================================
為什麼需要這支程式：

跑一輪 100 題只能得到一個數字（98 或 100），但兩輪之間 60/100 題會產生
不同的 SQL，失敗題目幾乎不重疊。也就是說 98 vs 100 的差距完全落在雜訊
裡 —— 用整輪分數去判斷「改了 prompt 有沒有變好」是在擲骰子。

生成模型是 MoE，temperature=0 也不決定性。要判斷一個修改的效果，正確
做法是固定題目、重複取樣，看命中率從 3/8 變成 7/8 這種級別的差異。
成本也低得多：8 次單題 ≈ 1 分鐘，一輪 100 題 ≈ 10 分鐘。

用法：
    python eval/eval_repeat.py 98              # 單題跑 8 次
    python eval/eval_repeat.py 93 98 --n 12    # 多題各跑 12 次
    python eval/eval_repeat.py --ids 70,72,91  # 逗號分隔也可以

輸出每題的 correct/N，並把不同的錯誤答案分組列出（同一種錯誤重複出現，
代表是穩定的系統性偏誤；每次錯得都不一樣，代表是取樣雜訊）。
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

from eval_score import GT_PATH, judge
from langgraph_sql.config import MYSQL_URI
from langgraph_sql.graph import build_graph
from langgraph_sql.utils.db_manager import get_db_manager
from test_runner import classify


def run_once(graph, question: str) -> dict:
    """跑一次 pipeline，回傳 judge() 需要的欄位。"""
    try:
        state = graph.invoke({"user_query": question, "retry_count": 0})
    except Exception as exc:
        return {"outcome": "crashed", "sql": "", "row_count": None,
                "error_message": f"{type(exc).__name__}: {exc}"}
    return {
        "outcome": classify(state),
        "sql": state.get("champion_sql") or "",
        "row_count": state.get("champion_row_count"),
        "retry": state.get("retry_count", 0),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*", type=int, help="題號")
    ap.add_argument("--ids", dest="id_csv", default="", help="逗號分隔的題號")
    ap.add_argument("--n", type=int, default=8, help="每題重複次數（預設 8）")
    ap.add_argument("--quiet", action="store_true", help="關掉 pipeline 的 log")
    args = ap.parse_args()

    ids = list(args.ids) + [int(x) for x in args.id_csv.split(",") if x.strip()]
    if not ids:
        ap.error("至少要給一個題號")

    if args.quiet:
        log.remove()

    gt = {e["id"]: e for e in yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))}
    missing = [i for i in ids if i not in gt]
    if missing:
        print(f"這些題號不在 {GT_PATH}: {missing}")
        return 1

    db = get_db_manager(MYSQL_URI)
    graph = build_graph()

    summary = []
    for qid in ids:
        entry = gt[qid]
        question = entry["question"]
        print(f"\n{'=' * 70}\n#{qid} {question}\n  期望: {entry['expect']}  ×{args.n} 次")

        verdicts: list[str] = []
        details: Counter = Counter()
        for k in range(args.n):
            result = run_once(graph, question)
            verdict, detail = judge(db, entry, result)
            verdicts.append(verdict)
            if verdict != "correct":
                # 用「第一行 + 系統回傳」當分組鍵：同一種錯會併在一起
                details[f"[{verdict}] {detail.strip()[:220]}"] += 1
            print(f"  {k + 1:>2}/{args.n}  {verdict}", flush=True)

        n_ok = verdicts.count("correct")
        print(f"\n  ==> {n_ok}/{args.n} 正確")
        for msg, cnt in details.most_common():
            print(f"      {cnt}x  {msg}")
        summary.append((qid, n_ok, args.n, question))

    print(f"\n{'=' * 70}\n總結（每題 {args.n} 次）:")
    for qid, n_ok, n, question in summary:
        bar = "#" * n_ok + "." * (n - n_ok)
        print(f"  #{qid:<4} {n_ok}/{n}  {bar}  {question[:40]}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
