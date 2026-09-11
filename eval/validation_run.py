# -*- coding: utf-8 -*-
"""兩組驗證集的跑法 —— 跟驗收集相反，這些組**就是要看逐題**。

驗收集用機制擋著不准看錯題；這組沒有那些機制，因為它的用途正是迭代。
界線寫在檔名跟 eval/validation_shapes.yaml 的檔頭裡：
    驗收集  跑完只看總分　　　→ eval/testset_run.py
    驗證集  跑完隨便看　　　　→ 這支

多做一件驗收集做不到的事：對每一題答錯的，再拿**系統的答案**去對 trap_sql。
對上了就是「掉進陷阱」—— 那是可歸因的機制失誤；沒對上是別的錯。
這兩種要分開數，因為只有前者能靠教 JOIN 語意修好。

用法：
    python eval/validation_run.py                 # 全跑
    python eval/validation_run.py --family fanout # 只跑一族
    python eval/validation_run.py --score-only    # 用最新的結果檔重新判分
"""
import argparse
import glob
import io
import json
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml

from eval_score import judge, match_ordered, match_unordered, to_rows
from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager
from testset_verify import shape_of

# 兩組驗證集共用這一支。差別只在題目怎麼出的：
#   shapes  照「錯題的 SQL 形狀」出 —— 結果 97.3%，證明我猜錯了成因
#   style   照「驗收集的問句風格」出 —— 短、口語、用生活話講概念
SETS = {
    "shapes": os.path.join(_ROOT, "eval", "validation_shapes.yaml"),
    "style": os.path.join(_ROOT, "eval", "validation_style.yaml"),
}
RES = os.path.join(_ROOT, "eval", "results")
QJSON = os.path.join(RES, "_validation_questions.json")
API_RETRY_ROUNDS = int(os.environ.get("VALIDATION_API_RETRY", "3"))


def commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT,
                             capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=_ROOT,
                               capture_output=True, text=True).stdout.strip()
        return out + ("+dirty" if dirty else "")
    except Exception:
        return "?"


def newest(which: str) -> str:
    """結果檔要照組別分開存 —— 不然 --score-only 會拿另一組的結果來判分。"""
    files = sorted(glob.glob(os.path.join(RES, "validation_%s_result_*.json" % which)))
    return files[-1] if files else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", dest="which", default="shapes", choices=sorted(SETS),
                    help="要跑哪一組驗證集")
    ap.add_argument("--family", default=None, help="只跑某一族（shapes 專用）")
    ap.add_argument("--score-only", action="store_true", help="不重跑，拿最新結果重新判分")
    ap.add_argument("--result", default=None, help="指定要判分的結果檔")
    args = ap.parse_args()

    path_yaml = SETS[args.which]
    entries = yaml.safe_load(io.open(path_yaml, encoding="utf-8"))
    # 沒有 family 欄的（style 組）就用 SQL 形狀分組 —— 切片要跟驗收集同一套算法。
    for e in entries:
        e.setdefault("family", shape_of(e.get("sql") or "") if e.get("sql") else "防禦")
    if args.family:
        entries = [e for e in entries if e.get("family") == args.family]
    by_id = {e["id"]: e for e in entries}
    print("驗證集［%s］%d 題　架構 commit %s\n" % (args.which, len(entries), commit()))

    if args.score_only or args.result:
        path = args.result or newest(args.which)
        if not path:
            print("找不到結果檔")
            return 1
    else:
        os.makedirs(RES, exist_ok=True)
        io.open(QJSON, "w", encoding="utf-8").write(json.dumps(
            [{"id": e["id"], "question": e["question"]} for e in entries],
            ensure_ascii=False, indent=2))
        from test_runner import run_evaluation  # noqa: E402
        before = set(glob.glob(os.path.join(RES, "eval_result_*.json")))
        run_evaluation(QJSON, None)
        made = sorted(set(glob.glob(os.path.join(RES, "eval_result_*.json"))) - before)
        if not made:
            print("跑完卻找不到新的結果檔")
            return 1
        path = made[-1]
        # API 故障要重試到底才判分 —— 那不是題目的結果，是網路的結果。
        for _ in range(API_RETRY_ROUNDS):
            prior = json.load(io.open(path, encoding="utf-8"))
            stuck = [r["id"] for r in prior if r.get("outcome") == "llm_api_error"]
            if not stuck:
                break
            print("\n%d 題 API 故障，重試：%s" % (len(stuck), stuck))
            before = set(glob.glob(os.path.join(RES, "eval_result_*.json")))
            run_evaluation(QJSON, path)
            made = sorted(set(glob.glob(os.path.join(RES, "eval_result_*.json"))) - before)
            if made:
                path = made[-1]
        target = os.path.join(RES, "validation_%s_result_%s.json"
                              % (args.which, os.path.basename(path)[13:-5]))
        io.open(target, "w", encoding="utf-8").write(io.open(path, encoding="utf-8").read())
        path = target

    results = {r["id"]: r for r in json.load(io.open(path, encoding="utf-8"))}
    db = get_db_manager(MYSQL_URI)

    tally, trapped, detail = {}, {}, []
    for e in entries:
        fam = e.get("family", "?")
        tally.setdefault(fam, [0, 0])
        trapped.setdefault(fam, 0)
        r = results.get(e["id"])
        if r is None:
            continue
        tally[fam][1] += 1
        verdict, why = judge(db, e, r)
        if verdict == "correct":
            tally[fam][0] += 1
            continue
        # 答錯了：是不是正好掉進陷阱？
        tag = ""
        trap = e.get("trap_sql")
        if trap and (r.get("sql") or "").strip():
            try:
                sys_rows = to_rows(db.execute_to_dataframe(r["sql"]))
                trap_rows = to_rows(db.execute_to_dataframe(trap))
                m = match_ordered if e.get("ordered") else match_unordered
                if m(sys_rows, trap_rows):
                    trapped[fam] += 1
                    tag = "【掉進陷阱】"
            except Exception:
                pass
        detail.append((fam, e["id"], tag, verdict, e["question"], why))

    print("=" * 74)
    tot = [sum(v[0] for v in tally.values()), sum(v[1] for v in tally.values())]
    for fam in sorted(tally):
        ok, n = tally[fam]
        print("  %-12s %3d/%-3d = %5.1f%%   其中掉進陷阱 %d 題"
              % (fam, ok, n, 100.0 * ok / n if n else 0, trapped[fam]))
    print("  %-12s %3d/%-3d = %5.1f%%" % ("總計", tot[0], tot[1],
                                          100.0 * tot[0] / tot[1] if tot[1] else 0))

    print("\n" + "=" * 74)
    print("逐題（這組就是要看的）")
    for fam, qid, tag, verdict, q, why in detail:
        print("\n  #%s [%s] %s%s" % (qid, fam, tag, verdict))
        print("     %s" % q)
        for line in str(why).split("\n"):
            print("     %s" % line.strip())
    print("\n結果檔 %s" % os.path.relpath(path, _ROOT))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
