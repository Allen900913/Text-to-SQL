"""
從 Ground Truth 反推每題「必須看得到」的表與欄位
==================================================
schema 一大就必須只把相關的表放進 Prompt。但剪枝是整條 pipeline 上唯一
沒有下游守門員的環節：模型幻覺欄位有 AST 擋、JOIN 寫錯有 EXPLAIN 擋，
而剪枝把 orders.status 剪掉時，模型根本不知道有這個欄位，於是「已完成的
訂單」這個條件就無聲消失 —— SQL 合法、EXPLAIN 通過、數字是錯的。

所以剪枝必須有獨立於 end-to-end 準確率的指標，否則準確率掉了也無從判斷
是檢索漏給、還是生成器變笨。這支程式提供那個指標的 ground truth：
GT SQL 用到哪些表與欄位，就是那一題「至少要看得到」的東西。

  Schema Recall = 剪枝後的 schema 完整涵蓋 GT 所需表與欄位的題數 / 總題數

這份需求集合是免費的 —— GT SQL 本來就要寫，順手解析一次就有。

用法：
    python eval_schema_need.py            # 印出分布統計與逐題需求
    python eval_schema_need.py --json     # 輸出機器可讀格式供剪枝器評分
"""
import json
import sys
from collections import Counter

import sqlglot
import yaml
from sqlglot import exp

from langgraph_sql.utils.schema_registry import get_table_columns

GT_PATH = "eval_ground_truth.yaml"


def required_schema(sql: str, known: dict[str, list[str]]) -> tuple[set[str], set[str]]:
    """
    解析一條 SQL，回傳 (需要的表, 需要的「表.欄位」)。

    無表限定詞的欄位（SELECT name）會依「哪張已知的表有這個欄位」回推；
    只有一張表有時可以確定歸屬，多張表都有時全部列入 —— 寧可高估需求，
    因為這份集合是用來檢查「剪枝有沒有漏給」，漏判的代價遠高於誤判。
    """
    ast = sqlglot.parse_one(sql, read="mysql")

    cte_names = {c.alias.lower() for c in ast.find_all(exp.CTE) if c.alias}
    alias_map: dict[str, str] = {}
    tables: set[str] = set()
    for node in ast.find_all(exp.Table):
        real = node.name.lower()
        if real in cte_names:
            continue
        tables.add(real)
        alias_map[real] = real
        if node.alias:
            alias_map[node.alias.lower()] = real

    columns: set[str] = set()
    for node in ast.find_all(exp.Column):
        col = node.name.lower()
        ref = (node.table or "").lower()
        real = alias_map.get(ref)
        if real:
            columns.add(f"{real}.{col}")
            continue
        # 無表限定詞：回推到有這個欄位的已知表（限定在本查詢用到的表之內）
        owners = [t for t in tables if col in known.get(t, [])]
        for t in owners:
            columns.add(f"{t}.{col}")

    return tables, columns


def main() -> int:
    as_json = "--json" in sys.argv
    gt = yaml.safe_load(open(GT_PATH, encoding="utf-8"))
    known = get_table_columns()

    need: dict[int, dict] = {}
    for entry in gt:
        if not entry.get("sql"):
            continue  # schema_unsupported 題沒有 GT SQL
        tables, columns = required_schema(entry["sql"], known)
        need[entry["id"]] = {
            "tables": sorted(tables),
            "columns": sorted(columns),
        }

    if as_json:
        print(json.dumps(need, ensure_ascii=False, indent=2))
        return 0

    tbl_dist = Counter(len(v["tables"]) for v in need.values())
    col_dist = Counter(len(v["columns"]) for v in need.values())
    tbl_freq = Counter(t for v in need.values() for t in v["tables"])
    col_freq = Counter(c for v in need.values() for c in v["columns"])

    print(f"可解析的題數: {len(need)}（另有 {len(gt) - len(need)} 題是防禦題，無 GT SQL）\n")

    print("每題需要幾張表:")
    for n in sorted(tbl_dist):
        print(f"  {n} 張  {tbl_dist[n]:>3} 題  {'█' * tbl_dist[n]}")
    print("\n每題需要幾個欄位:")
    for n in sorted(col_dist):
        print(f"  {n:>2} 個  {col_dist[n]:>3} 題  {'█' * col_dist[n]}")

    print(f"\n表被需要的次數（共 {len(tbl_freq)} 張）:")
    for t, n in tbl_freq.most_common():
        print(f"  {t:<14} {n:>3} 題")

    print(f"\n最常被需要的 12 個欄位（共 {len(col_freq)} 個）:")
    for c, n in col_freq.most_common(12):
        print(f"  {c:<26} {n:>3} 題")

    unused = sorted(
        f"{t}.{c}" for t, cols in known.items() for c in cols
        if f"{t}.{c}" not in col_freq
    )
    print(f"\n完全沒有題目需要的欄位 ({len(unused)}): {', '.join(unused) or '無'}")

    total_tables = len(known)
    print(f"\n若剪枝目標是「涵蓋 GT 所需的表」，"
          f"平均每題只需 {sum(len(v['tables']) for v in need.values()) / len(need):.1f}"
          f" / {total_tables} 張表")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
