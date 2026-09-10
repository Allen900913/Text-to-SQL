# -*- coding: utf-8 -*-
"""跑保留驗收集，並且**用機制擋住污染**。

協定
================================================================
這份題庫只回答一個問題：「這個架構在沒參與過任何決策的題上有多準」。
所以它跟開發集（`eval_ground_truth.yaml`）的界線是硬的：

    跑完只准看**總分與事前登記的切面**（表數／形狀／寬窄）。
    錯題清單不准拿來改任何東西 —— 註解、參數、few-shot、檢索都不行。
    看了就污染了，這份題庫的價值就沒了。

要迭代就回去用開發集。那組本來就是拿來調的。

這支程式做四件事，讓上面那句話不只是承諾：
    ① 對 hash —— 題庫被改過就拒跑（改過就不是同一把尺）
    ② **預設不輸出逐題結果**。逐題要加 `--show-failures`，而那個旗標會被
       寫進執行紀錄，之後看得到誰在什麼時候看過
    ③ 每跑一次自動寫一筆紀錄：架構 commit、分數、日期、有沒有看逐題
    ④ 同一個 commit 已經跑過就擋 —— 重跑同一版只會誘發挑數字

封存之後才發現某題 GT 錯了怎麼辦
----------------------------------------------------------------
那題**作廢**：把題號加進 runlog 的 `void`、寫明理由，之後的分母自動扣掉。
不是修好再跑一次 —— 修完再跑等於用結果挑題目。

用法：
    python eval/testset_verify.py              # 封存前：六項檢查要全綠
    python eval/testset_run.py --seal          # 封存 hash
    python eval/testset_run.py                 # 跑一次，只印總分與切面
"""
import argparse
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import datetime

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (_ROOT, os.path.join(_ROOT, "eval")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import yaml  # noqa: E402

TESTSET = os.path.join(_ROOT, "eval", "testset_holdout.yaml")
RUNLOG = os.path.join(_ROOT, "eval", "testset_runlog.json")
_QJSON = os.path.join(_ROOT, "eval", "results", "_testset_questions.json")


def content_hash():
    return hashlib.sha256(
        io.open(TESTSET, encoding="utf-8").read().encode("utf-8")).hexdigest()[:16]


def git_commit():
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=_ROOT,
                           capture_output=True, text=True)
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=_ROOT,
                               capture_output=True, text=True).stdout.strip()
        return (r.stdout.strip() or "?") + ("+dirty" if dirty else "")
    except Exception:
        return "?"


def load_log():
    if os.path.exists(RUNLOG):
        return json.load(io.open(RUNLOG, encoding="utf-8"))
    return {"sealed_hash": None, "sealed_on": None, "void": [], "runs": []}


def save_log(log):
    io.open(RUNLOG, "w", encoding="utf-8").write(
        json.dumps(log, ensure_ascii=False, indent=2))


def slices(entry, known):
    """事前登記的切面。逐題不給，切面給 —— 切面看不出是哪一題錯的。"""
    sql = entry.get("sql") or ""
    ts = {x.lower() for x in
          re.findall(r"(?:FROM|JOIN)\s+`?([a-zA-Z_][a-zA-Z0-9_]*)`?", sql, re.I)
          if x.lower() in known}
    s = sql.upper()
    shape = ("聚合" if re.search(r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\(", s) else "") \
        + ("+視窗" if "OVER" in s else "") \
        + ("+子查詢" if s.count("SELECT") > 1 else "") \
        + ("+否定" if re.search(r"NOT IN|NOT EXISTS", s) else "")
    return {"表數": len(ts) if ts else "—",
            "形狀": shape or ("—" if not sql else "純投影/過濾"),
            "寬窄": "寬表" if any(t.endswith("_profiles") for t in ts) else "窄表",
            "expect": str(entry.get("expect") or "rows")}


def seal(new_version=False, reason=""):
    """封存。改過內容要換版，而且**舊版的紀錄不能被覆蓋**。

    為什麼要有換版這條路：量尺本身可能有缺陷。v1 封存時漏跑了閘門 [13]
    （題目形狀可判定性），事後補跑掃到 17 題問句決定不了 GT 的 SELECT 清單 ——
    判分規則要求系統涵蓋 GT 的每一欄，那 17 題會讓模型「值算對、形狀不合」
    而被判錯，那是題目的缺陷不是模型的。

    但換版**不准重算舊版的分數**。拿已知的分數去挑掉題目再算一次，正是
    這整套守衛要防的事。所以舊版連同它的 runs 整組搬進 `retired`，
    新版的 runs 從零開始，兩者永遠分開讀。
    """
    log = load_log()
    h = content_hash()
    if log["sealed_hash"] == h:
        print("內容沒變（hash %s），不必重新封存。" % h)
        return 0
    if log["sealed_hash"] and not new_version:
        print("✗ 已經封存過（%s），現在的內容是 %s —— 題庫被改過。"
              % (log["sealed_hash"], h))
        print("  改過就不是同一把尺。要嘛還原，要嘛用 --new-version 換版")
        print("  （換版要附 --reason，而且舊版的分數**不會**被重算）。")
        return 1
    qs = yaml.safe_load(io.open(TESTSET, encoding="utf-8"))
    if log["sealed_hash"]:
        if not reason:
            print("✗ 換版要說明為什麼 —— 加 --reason \"...\"。")
            return 1
        log.setdefault("retired", []).append({
            "version": log.get("version", 1),
            "hash": log["sealed_hash"], "sealed_on": log["sealed_on"],
            "n": log.get("n_questions"), "runs": log["runs"],
            "retired_on": datetime.now().strftime("%Y-%m-%d"), "reason": reason,
        })
        log["runs"] = []
        log["version"] = log.get("version", 1) + 1
    else:
        log["version"] = 1
    log["sealed_hash"] = h
    log["sealed_on"] = datetime.now().strftime("%Y-%m-%d")
    log["n_questions"] = len(qs)
    save_log(log)
    print("已封存 v%d　%d 題　hash=%s　日期 %s"
          % (log["version"], len(qs), h, log["sealed_on"]))
    if log.get("retired"):
        r = log["retired"][-1]
        print("v%d 連同它的 %d 次執行已移進 retired，分數保持原樣不重算。"
              % (r["version"], len(r["runs"])))
    print("從現在起這個檔不要再改 —— 改了會拒跑。")
    return 0


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--seal", action="store_true", help="封存 hash")
    ap.add_argument("--new-version", action="store_true",
                    help="題庫改過，換版重新封存（舊版連同分數搬進 retired，不重算）")
    ap.add_argument("--reason", default="", help="換版的理由，會寫進 retired")
    ap.add_argument("--show-failures", action="store_true",
                    help="印出逐題錯誤 —— **會污染這份題庫**，而且會記進 runlog")
    ap.add_argument("--force", action="store_true", help="同一個 commit 仍要重跑")
    ap.add_argument("--resume", default=None, help="接續既有的 eval_result_*.json")
    args = ap.parse_args(argv)

    if args.seal:
        return seal(args.new_version, args.reason)

    log = load_log()
    if not log["sealed_hash"]:
        print("還沒封存。先跑 eval/testset_verify.py 確認全綠，再跑 --seal。")
        return 1
    h = content_hash()
    if h != log["sealed_hash"]:
        print("✗ 題庫內容 %s 與封存時的 %s 不同 —— 拒跑。" % (h, log["sealed_hash"]))
        print("  封存之後題目只能作廢（寫進 runlog 的 void），不能修改。")
        return 1
    commit = git_commit()
    if [r for r in log["runs"] if r.get("commit") == commit] and not args.force:
        prev = [r["score"] for r in log["runs"] if r.get("commit") == commit]
        print("⚠ 這個 commit（%s）已經跑過，分數 %s。" % (commit, prev))
        print("  同一版重跑只會誘發挑數字。先改架構、換 commit，再跑。"
              "（真的要重跑：--force）")
        return 1

    qs = yaml.safe_load(io.open(TESTSET, encoding="utf-8"))
    void = set(log.get("void") or [])
    live = [q for q in qs if q["id"] not in void]
    print("驗收集 %d 題（作廢 %d）　hash %s　架構 commit %s"
          % (len(live), len(void), h, commit))

    os.makedirs(os.path.dirname(_QJSON), exist_ok=True)
    io.open(_QJSON, "w", encoding="utf-8").write(json.dumps(
        [{"id": q["id"], "question": q["question"]} for q in live],
        ensure_ascii=False, indent=2))

    from test_runner import run_evaluation  # noqa: E402
    run_evaluation(_QJSON, args.resume)

    res_dir = os.path.join(_ROOT, "eval", "results")
    newest = max((os.path.join(res_dir, f) for f in os.listdir(res_dir)
                  if f.startswith("eval_result_")), key=os.path.getmtime)
    results = {r["id"]: r for r in json.load(io.open(newest, encoding="utf-8"))}

    from eval_score import judge  # noqa: E402
    from langgraph_sql.utils.db_manager import get_db_manager
    from langgraph_sql.utils.value_index import MYSQL_URI
    db = get_db_manager(MYSQL_URI)
    known = {t.lower() for t in db.get_table_names()} \
        if hasattr(db, "get_table_names") else set()
    if not known:
        from langgraph_sql.utils.schema_registry import get_table_columns
        known = {t.lower() for t in get_table_columns()}

    ok, verdicts, by = 0, Counter(), {}
    fails = []
    for q in live:
        r = results.get(q["id"])
        if r is None:
            verdicts["missing"] += 1
            continue
        v, detail = judge(db, q, r)
        verdicts[v] += 1
        ok += (v == "correct")
        for k, val in slices(q, known).items():
            d = by.setdefault(k, {}).setdefault(val, [0, 0])
            d[1] += 1
            d[0] += (v == "correct")
        if v != "correct":
            fails.append((q["id"], v, detail[:120]))

    n = len(live)
    score = round(100.0 * ok / n, 1) if n else 0.0
    print()
    print("=" * 58)
    print("總分　%d / %d　= %.1f%%" % (ok, n, score))
    print("判定　%s" % dict(verdicts))
    print()
    for k in ("表數", "形狀", "寬窄", "expect"):
        print("[%s]" % k)
        for val, (c, t) in sorted(by.get(k, {}).items(), key=lambda kv: str(kv[0])):
            print("    %-14s %3d/%-3d  %5.1f%%" % (val, c, t, 100.0 * c / t))
    print("=" * 58)

    if args.show_failures:
        print("\n⚠ 逐題錯誤 —— 看了這份清單就不能再用它改架構了。"
              "這次查看已記進 runlog。")
        for qid, v, d in fails:
            print("   #%s  %s  %s" % (qid, v, d))
    else:
        print("\n逐題結果不輸出（協定）。要迭代請回開發集 eval_ground_truth.yaml。")

    log["runs"].append({
        "date": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "commit": commit, "score": score, "correct": ok, "n": n,
        "verdicts": dict(verdicts), "result_file": os.path.basename(newest),
        "viewed_failures": bool(args.show_failures),
        "testset_version": log.get("version", 1),
    })
    save_log(log)
    print("已記入 %s" % os.path.basename(RUNLOG))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main(sys.argv[1:]))
