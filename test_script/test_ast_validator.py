"""AST 驗證器的迴歸測試 —— 重點在「合法 SQL 不可以被擋下來」

為什麼特別測這件事（ARCHITECTURE.md §6.8）：

    這一層是 fail-open 設計 —— 解析不出歸屬的欄位一律放行，因為
    **誤擋的代價遠高於漏擋**：漏擋的 SQL 後面還有 EXPLAIN 與實際執行接住，
    誤擋卻會吃掉一次重試，重試預算燒光就變成 error_end（連答案都沒有）。

    `#17` 就是這樣壞的：`SELECT p.*` 被 sqlglot 解析成
    Column(name='*', table='p')，驗證器拿 '*' 去比對 products 的欄位清單，
    判成幻覺欄位 → 三次重試全部被同一個誤判擋掉 → error_end。

    所以這支測試的主體是**誤判**，不是漏判。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8")

from loguru import logger as log  # noqa: E402

log.remove()

from langgraph_sql.nodes.ast_validator import ast_validator  # noqa: E402

PASSES = [
    # (說明, SQL)
    ("帶表限定的星號（#17 的壞法）", "SELECT p.* FROM products AS p"),
    ("裸星號", "SELECT * FROM products"),
    ("COUNT(*)", "SELECT COUNT(*) AS n FROM products AS p"),
    ("星號 + 具名欄位混用", "SELECT p.*, c.name FROM products AS p, categories AS c"),
    ("JOIN 後的星號", "SELECT o.* FROM orders AS o JOIN customers AS c ON c.id = o.customer_id"),
    ("CTE 裡的星號", "WITH t AS (SELECT p.* FROM products AS p) SELECT * FROM t"),
    ("一般具名欄位", "SELECT p.name, p.price FROM products AS p"),
]

BLOCKS = [
    ("不存在的表", "SELECT x.id FROM not_a_table AS x"),
    ("不存在的欄位", "SELECT p.profit_margin FROM products AS p"),
]


def run(sql: str) -> tuple[bool, str]:
    state = {"user_query": "t", "candidate_sqls": [sql], "retry_count": 0}
    out = ast_validator(state)
    err = out.get("db_error") or ""
    return (not err), err


def main() -> int:
    bad = 0
    for label, sql in PASSES:
        ok, err = run(sql)
        if not ok:
            bad += 1
            print(f"  ✗ 應放行卻被擋：{label}\n      {sql}\n      {err[:120]}")
    for label, sql in BLOCKS:
        ok, _err = run(sql)
        if ok:
            bad += 1
            print(f"  ✗ 應攔截卻放行：{label}\n      {sql}")
    total = len(PASSES) + len(BLOCKS)
    if bad:
        print(f"ast_validator 測試失敗 {bad}/{total} 項")
        return 1
    print(f"ast_validator 測試全數通過（{total} 項）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
