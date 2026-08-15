"""
Ground Truth 對帳 — 算出真正的正確率
=====================================
完成率（ok / empty / schema_unsupported / error_end）量的是「有沒有跑出東西」。
這支量的是「答得對不對」：把系統產生的 SQL 與 eval_ground_truth.yaml 的
預期答案各自執行一次，比對結果集。

用法：
    python eval_score.py                    # 取最新的 eval_result_*.json
    python eval_score.py eval_result_X.json

比對規則（刻意寬鬆，只抓真正的錯）：
  - 忽略欄位名稱與欄位順序。系統常會多帶 id / email 等欄位，
    只要 GT 那幾個值都在同一列裡出現就算對。
  - 數值統一四捨五入到小數 2 位；午夜的 datetime 視同 date。
  - 筆數必須相同。多回或少回都是錯 —— #25 回 6 個城市而正確答案是 0 筆，
    正是靠這條抓出來的。
  - ordered: true 的題目（Top-N、明確要求排序）另外檢查順序。
  - 題目本身語意有歧義時，GT 會列出 alt_sql，任一相符即通過。
"""
import glob
import json
import sys
from collections import Counter
from datetime import date, datetime
from decimal import Decimal

import yaml

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager

GT_PATH = "eval_ground_truth.yaml"


# ===========================================================================
# 正規化與比對
# ===========================================================================

def norm_val(v) -> str:
    """把單一儲存格正規化成可比對的字串。"""
    if v is None or (isinstance(v, float) and v != v):  # None / NaN
        return "NULL"
    if isinstance(v, bool):
        return str(int(v))
    if isinstance(v, (int, float, Decimal)):
        f = float(v)
        return str(int(f)) if f == int(f) else f"{f:.2f}"
    if isinstance(v, datetime):
        # 午夜視同純日期：GT 取 created_at，系統可能取 DATE(created_at)，
        # 兩者指的是同一天，不該因為 00:00:00 的有無判成不同。
        return v.date().isoformat() if (v.hour, v.minute, v.second) == (0, 0, 0) \
            else v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    s = str(v).strip()
    if s.endswith(" 00:00:00"):
        return s[:-9]
    return s


def row_counter(row) -> Counter:
    """一列 → 值的多重集合。用多重集合而非 set，才不會把 (5, 5) 併成 (5)。"""
    return Counter(norm_val(v) for v in row)


def to_rows(df) -> list[Counter]:
    return [row_counter(r) for r in df.itertuples(index=False)]


def row_covers(sys_row: Counter, gt_row: Counter) -> bool:
    """系統這一列是否涵蓋 GT 這一列的所有值（允許系統多帶欄位）。"""
    return all(sys_row[k] >= n for k, n in gt_row.items())


def match_unordered(sys_rows: list[Counter], gt_rows: list[Counter]) -> bool:
    """
    不計順序的一對一配對。先試完全相等（絕大多數情況），
    再退回貪婪涵蓋配對處理「系統多帶欄位」的情形。
    """
    if len(sys_rows) != len(gt_rows):
        return False
    if Counter(map(frozenset_of, sys_rows)) == Counter(map(frozenset_of, gt_rows)):
        return True
    unused = list(sys_rows)
    for gt_row in sorted(gt_rows, key=lambda c: -sum(c.values())):
        for i, sys_row in enumerate(unused):
            if row_covers(sys_row, gt_row):
                unused.pop(i)
                break
        else:
            return False
    return True


def frozenset_of(c: Counter) -> frozenset:
    return frozenset(c.items())


def match_ordered(sys_rows: list[Counter], gt_rows: list[Counter]) -> bool:
    return len(sys_rows) == len(gt_rows) and all(
        row_covers(s, g) for s, g in zip(sys_rows, gt_rows)
    )


# ===========================================================================
# 逐題判定
# ===========================================================================

def judge(db, entry: dict, result: dict) -> tuple[str, str]:
    """回傳 (verdict, detail)。verdict ∈ {correct, wrong, no_sql}。"""
    expect = entry["expect"]
    outcome = result.get("outcome", "")
    sql = (result.get("sql") or "").strip()

    if outcome in ("error_end", "llm_api_error") or not sql:
        return "no_sql", f"系統沒有產出 SQL（outcome={outcome}）"

    if expect == "schema_unsupported":
        if outcome == "schema_unsupported":
            return "correct", ""
        return "wrong", f"應觸發防禦暗號，實際 outcome={outcome}，回了 {result.get('row_count')} 列"

    if outcome == "schema_unsupported":
        return "wrong", "誤觸防禦暗號 —— 這題用現有 schema 答得出來"

    try:
        sys_df = db.execute_to_dataframe(sql)
    except Exception as exc:
        return "no_sql", f"系統 SQL 重跑失敗: {type(exc).__name__}: {exc}"

    if expect == "empty":
        if len(sys_df) == 0:
            return "correct", ""
        return "wrong", f"正確答案是 0 筆，系統回了 {len(sys_df)} 筆: " \
                        f"{[list(r) for r in sys_df.head(4).itertuples(index=False)]}"

    sys_rows = to_rows(sys_df)
    matcher = match_ordered if entry.get("ordered") else match_unordered

    candidates = [entry["sql"]] + list(entry.get("alt_sql") or [])
    for cand in candidates:
        gt_df = db.execute_to_dataframe(cand)
        if matcher(sys_rows, to_rows(gt_df)):
            return "correct", ""

    gt_df = db.execute_to_dataframe(entry["sql"])
    return "wrong", (
        f"系統 {len(sys_df)} 列 / GT {len(gt_df)} 列\n"
        f"        系統: {[list(r) for r in sys_df.head(3).itertuples(index=False)]}\n"
        f"        GT  : {[list(r) for r in gt_df.head(3).itertuples(index=False)]}"
    )


def main() -> int:
    path = sys.argv[1] if len(sys.argv) > 1 else sorted(glob.glob("eval_result_*.json"))[-1]
    print(f"對帳檔案: {path}\n")

    gt = {e["id"]: e for e in yaml.safe_load(open(GT_PATH, encoding="utf-8"))}
    results = {int(r["id"]): r for r in json.load(open(path, encoding="utf-8"))}
    db = get_db_manager(MYSQL_URI)

    tally: Counter = Counter()
    failures: list[str] = []

    for qid in sorted(gt):
        entry = gt[qid]
        result = results.get(qid)
        if result is None:
            tally["missing"] += 1
            failures.append(f"#{qid:<3} [missing] 結果檔裡沒有這題")
            continue
        verdict, detail = judge(db, entry, result)
        tally[verdict] += 1
        if verdict != "correct":
            failures.append(f"#{qid:<3} [{verdict}] {entry['question']}\n        {detail}")

    total = sum(tally.values())
    correct = tally["correct"]
    print(f"{'=' * 70}")
    if failures:
        print(f"不符 {len(failures)} 題:\n")
        print("\n".join(failures))
        print(f"\n{'=' * 70}")
    print(f"正確率: {correct}/{total} = {correct / total * 100:.1f}%")
    print("  " + "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    return 0 if correct == total else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
