"""穩定性分層 —— 一個分數不夠，要知道哪些題是在擲硬幣

為什麼需要這支（ARCHITECTURE.md §5.2、§9.3）：

    gpt-oss 是 MoE，temperature=0 仍然不確定。單輪 159 題只給一個數字，
    而那個數字會把「穩定答對」與「八次中四次」混為一談。

    #56 就是這樣被掩蓋的：上一輪 144/145 時它剛好擲中，看起來完全正常；
    重複取樣才發現是 4/8，而且四次錯的答案完全相同（0.2438）——
    是穩定的系統性偏誤，不是雜訊。

做法：把題目分三類。不必每題跑 n 次 —— 只跑「上一輪錯的」加上一組隨機
抽樣（抽樣是為了發現還沒被抓到的擲硬幣題），成本可控。

    穩定過   n/n      —— 但 n=8 分不出 7/8 與 8/8，別過度解讀
    擲硬幣   0<x<n    —— 真正要處理的一類：分數會隨機漂移
    穩定錯   0/n      —— 系統性缺陷，可歸因、可修

用法：
    python eval/eval_stability.py                      # 最新結果的錯題 + 隨機 15 題
    python eval/eval_stability.py --sample 25 --n 8
    python eval/eval_stability.py --ids 56,119,138     # 指定題號
    python eval/eval_stability.py --wrong-only         # 只跑上一輪錯的
"""
import os as _os
import sys as _sys

# 搬進子目錄之後要自己把專案根目錄放進 sys.path（tools/ 也是這個寫法）。
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_RESULTS = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "results")

import sys

# 必須在 import langgraph_sql 之前 —— 匯入時就會有 log 輸出，
# cp950 主控台碰到 emoji 會噴 UnicodeEncodeError（不致命但很吵）
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import argparse
import glob
import io
import json
import random
from collections import Counter

import yaml
from loguru import logger as log

from eval_repeat import run_once
from eval_score import GT_PATH, judge
from langgraph_sql.config import MYSQL_URI
from langgraph_sql.graph import build_graph
from langgraph_sql.utils.db_manager import get_db_manager


def wrong_ids(db, gt: dict, path: str) -> list[int]:
    results = {int(r["id"]): r for r in json.load(io.open(path, encoding="utf-8"))}
    out = []
    for qid, entry in gt.items():
        r = results.get(qid)
        if r is None:
            continue
        verdict, _ = judge(db, entry, r)
        if verdict != "correct":
            out.append(qid)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("result", nargs="?", help="eval_result_*.json（預設取最新）")
    ap.add_argument("--n", type=int, default=8, help="每題重複次數（預設 8）")
    ap.add_argument("--sample", type=int, default=15, help="額外隨機抽幾題（預設 15）")
    ap.add_argument("--ids", default="", help="逗號分隔的題號，指定後忽略 --sample")
    ap.add_argument("--wrong-only", action="store_true", help="只跑上一輪錯的")
    ap.add_argument("--seed", type=int, default=0, help="抽樣種子，固定才能跨輪比較")
    ap.add_argument("--out", default=None,
                    help="把逐題 n_ok 寫成 JSON（A/B 要對帳就一定要給）。"
                         "檔案已存在就**接著跑**，跳過已完成的題目。")
    args = ap.parse_args()

    log.remove()
    # log.remove() 之後 [Node 3] 的 WARNING 就不會進 log 了 —— 靠 grep 日誌判斷
    # 某一層有沒有作用會恆為 0（§9.14 四之三）。而且環境變數相同不代表行為相同：
    # A/B 跑到一半改了被測檔案，同一個旗標在前後兩版指向不同模式（四之六）。
    # 所以這裡印的是**行程內實際生效的模式**，不是傳進來的旗標。
    from langgraph_sql.nodes.ast_validator import SCOPE_MODE
    from langgraph_sql.utils.schema_parser import get_schema_parser
    print(f"[驗證器] 本行程實際生效 SCOPE_MODE = {SCOPE_MODE}")
    # 同一條教訓套在 prompt 層：問**載進來的 parser**，不要問環境變數。
    _p = get_schema_parser()
    _arm = {"semantic_layer": _p.path,
            "n_enum_fields": len(_p._data.get("enum_fields") or {}),
            "scope_mode": SCOPE_MODE}
    print(f"[Prompt] 實際載入 {_arm['semantic_layer']}"
          f"｜enum_fields {_arm['n_enum_fields']} 項")
    gt = {e["id"]: e for e in yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))}
    db = get_db_manager(MYSQL_URI)

    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        source = "指定"
    else:
        path = args.result or sorted(glob.glob(_os.path.join(_RESULTS, "eval_result_*.json")))[-1]
        print(f"上一輪結果: {path}")
        bad = wrong_ids(db, gt, path)
        print(f"上一輪答錯 {len(bad)} 題: {bad}")
        ids = list(bad)
        if not args.wrong_only:
            pool = [q for q in gt if q not in set(bad)]
            random.Random(args.seed).shuffle(pool)
            ids += sorted(pool[:args.sample])
        source = "錯題 + 隨機抽樣"

    # 續跑：12 小時的 A/B 中途一定會被打斷（配額、機器、手滑）。
    # 重跑整臂不只是浪費配額，還會讓兩臂落在不同的時間窗上
    # （[[baselines-die-when-the-model-changes.md]]）。
    done: dict = {}
    if args.out and _os.path.exists(args.out):
        prev = json.load(io.open(args.out, encoding="utf-8"))
        assert prev["arm"] == _arm, (
            f"續跑的臂跟這個行程不一樣，中止：\n  檔案 {prev['arm']}\n  行程 {_arm}")
        assert prev["n"] == args.n, f"續跑的 --n 不同（{prev['n']} vs {args.n}），中止"
        done = {int(k): v for k, v in prev["n_ok"].items()}
        print(f"續跑 {args.out}：已完成 {len(done)} 題，跳過")

    # SQL 也要存。判準 [4]（機制：非法字面值還出不出現）沒有 SQL 就驗不了，
    # 而重跑一次只為了拿 SQL 是白燒 12 小時的配額。
    sqls: dict = {}
    if args.out and _os.path.exists(args.out):
        sqls = {int(k): v for k, v in
                json.load(io.open(args.out, encoding="utf-8")).get("sql", {}).items()}

    def _save():
        if not args.out:
            return
        io.open(args.out, "w", encoding="utf-8", newline="\n").write(json.dumps(
            {"arm": _arm, "n": args.n, "n_ok": done, "sql": sqls},
            ensure_ascii=False, indent=1))

    todo = [q for q in ids if q not in done]
    print(f"\n來源: {source}｜{len(todo)} 題 × {args.n} 次 = {len(todo) * args.n} 次 pipeline\n")
    graph = build_graph()

    rows = []
    for qid in todo:
        entry = gt[qid]
        details: Counter = Counter()
        n_ok = 0
        for _ in range(args.n):
            res = run_once(graph, entry["question"])
            sqls.setdefault(qid, []).append(res.get("sql") or "")
            verdict, detail = judge(db, entry, res)
            if verdict == "correct":
                n_ok += 1
            else:
                details[f"[{verdict}] {detail.strip()[:160]}"] += 1
        if n_ok == args.n:
            klass = "穩定過"
        elif n_ok == 0:
            klass = "穩定錯"
        else:
            klass = "擲硬幣"
        rows.append((qid, n_ok, klass, entry["question"], details))
        bar = "#" * n_ok + "." * (args.n - n_ok)
        print(f"  #{qid:<4} {n_ok}/{args.n} {bar}  {klass}  {entry['question'][:34]}",
              flush=True)
        # 每題就存一次 —— 存在最後等於沒有續跑。
        done[qid] = n_ok
        _save()

    print(f"\n{'=' * 72}")
    # 統計要看 `done` 不是 `rows` —— 續跑時 rows 只有這次新跑的那幾題，
    # 印出來會是一份看起來正常、其實少了一半的摘要（[[silent-pass-is-not-a-pass]]）。
    tally = Counter("穩定過" if v == args.n else "穩定錯" if v == 0 else "擲硬幣"
                    for v in done.values()) if done else Counter(
        k for _, _, k, _, _ in rows)
    print(f"  合計 {sum(tally.values())} 題"
          + ("（含續跑前已完成的）" if len(done) > len(rows) else ""))
    for k in ("穩定過", "擲硬幣", "穩定錯"):
        print(f"  {k}: {tally[k]}")
    if done:
        print(f"  總正確樣本 {sum(done.values())}/{len(done) * args.n}"
              f" = {sum(done.values()) / (len(done) * args.n):.1%}")

    for klass, title in (("穩定錯", "穩定錯 —— 系統性缺陷，可歸因"),
                         ("擲硬幣", "擲硬幣 —— 分數會隨機漂移，單輪計分不可信")):
        picked = [r for r in rows if r[2] == klass]
        if not picked:
            continue
        print(f"\n{title}:")
        for qid, n_ok, _k, question, details in picked:
            print(f"  #{qid} {n_ok}/{args.n}  {question}")
            for msg, cnt in details.most_common(3):
                # 同一種錯反覆出現 = 穩定偏誤；每次都不同 = 取樣雜訊
                print(f"      {cnt}x  {msg}")

    print("\n注意：n=8 分辨得出 3/8 與 7/8，分不出 7/8 與 8/8（§5.2）。")
    print("      「穩定過」只代表這 n 次都對，不是保證。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
