"""
Ground Truth 自檢
==================
只驗證 eval_ground_truth.yaml 本身是否健康，不碰系統輸出：
  1. 每題都有、且只有一筆 GT，id 與題目文字與 eval_questions_v2.json 一致
  2. 每條 GT SQL（含 alt_sql）都跑得動
  3. expect: rows 的題目確實有結果；expect: empty 的確實是 0 筆
  4. expect: schema_unsupported 的題目不該有 sql

GT 自己寫錯是最糟的情況 —— 它會把正確答案判成錯的，而且沒有第二道防線。
所以每次改動 GT 或重新 seed 資料，都要先讓這支跑過。
"""
import sys

import yaml

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager

GT_PATH = "eval_ground_truth.yaml"
Q_PATH = "eval_questions_v2.json"


def main() -> int:
    import json

    gt = yaml.safe_load(open(GT_PATH, encoding="utf-8"))
    questions = {q["id"]: q["question"] for q in json.load(open(Q_PATH, encoding="utf-8"))}
    db = get_db_manager(MYSQL_URI)

    problems: list[str] = []
    seen: set[int] = set()
    stats = {"rows": 0, "empty": 0, "schema_unsupported": 0}

    for entry in gt:
        qid = entry["id"]
        if qid in seen:
            problems.append(f"#{qid} 重複定義")
        seen.add(qid)

        if qid not in questions:
            problems.append(f"#{qid} 不在題庫裡")
        elif questions[qid] != entry["question"]:
            problems.append(f"#{qid} 題目文字與題庫不一致\n"
                            f"      題庫: {questions[qid]}\n"
                            f"      GT  : {entry['question']}")

        expect = entry["expect"]
        stats[expect] = stats.get(expect, 0) + 1

        if expect == "schema_unsupported":
            if entry.get("sql"):
                problems.append(f"#{qid} 標為 schema_unsupported 卻寫了 sql")
            continue

        if not entry.get("sql"):
            problems.append(f"#{qid} 缺少 sql")
            continue

        for label, sql in [("sql", entry["sql"])] + [
            (f"alt_sql[{i}]", s) for i, s in enumerate(entry.get("alt_sql") or [])
        ]:
            try:
                df = db.execute_to_dataframe(sql)
            except Exception as exc:
                problems.append(f"#{qid} {label} 執行失敗: {type(exc).__name__}: {exc}")
                continue
            if label == "sql":
                if expect == "rows" and len(df) == 0:
                    problems.append(f"#{qid} 預期有結果卻是 0 筆 —— GT 寫錯，或題目已與資料脫節")
                if expect == "empty" and len(df) != 0:
                    problems.append(f"#{qid} 預期 0 筆卻回了 {len(df)} 筆 —— 陷阱題已失效")
                print(f"  #{qid:<3} {expect:<18} {len(df):>3} 列 x {len(df.columns)} 欄")

    missing = sorted(set(questions) - seen)
    if missing:
        problems.append(f"題庫有但 GT 沒有: {missing}")

    print(f"\n{'=' * 66}")
    print(f"GT 題數 {len(gt)} / 題庫 {len(questions)}   " +
          "  ".join(f"{k}={v}" for k, v in stats.items()))
    if problems:
        print(f"\n❌ {len(problems)} 個問題:")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("\n✅ GT 自檢全數通過")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
