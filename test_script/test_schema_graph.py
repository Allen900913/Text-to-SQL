"""
schema_graph 的回歸測試 —— 鎖住 KMB + 表權重的行為
====================================================================
為什麼需要這支測試：

_TABLE_COST 的值不是隨手挑的，3 是唯一同時滿足上下界的數字（推導見
schema_graph.py 的註解）。調成 2 會讓 browse_logs 捷徑與購買路徑並列而
修不掉；調成 4 會讓「購物車裡有什麼商品」繞道 orders。兩種錯誤都不會拋
例外，只會讓模型收到語意錯誤的表集合，然後自信地回答錯的東西。

實測 #133 就是這個錯誤的後果：瀏覽最多的是洋芋片、銷量最高的是泡麵、
營收最高的是 MacBook Air —— 走錯路會得到完全不同的答案。

執行：python test_script/test_schema_graph.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger as log  # noqa: E402

from langgraph_sql.utils.schema_graph import (  # noqa: E402
    find_join_path,
    get_join_graph,
    table_cost,
)

# 行為 / 紀錄類的表：拓撲上把 customers 與 products 連起來，但語意上
# 「看過」「想買」都不等於「買了」。
BEHAVIOURAL = {"browse_logs", "reviews", "carts", "cart_items"}

# (錨點, 必須出現的表, 說明)
MUST_INCLUDE = [
    (["customers", "products"], {"orders", "order_items"},
     "問客戶與商品的關聯要走購買路徑"),
    (["customers", "suppliers"], {"orders", "order_items"},
     "客戶到供應商一樣得經過購買"),
    (["customers", "categories"], {"orders", "order_items"},
     "客戶到分類一樣得經過購買"),
    (["cart_items", "customers"], {"carts"},
     "購物車明細到客戶的橋樑是 carts，不可繞道訂單"),
    (["carts", "products"], {"cart_items"},
     "購物車到商品的橋樑是 cart_items，不可繞道訂單"),
]

# (錨點, 不可出現的表) —— 錨點自己不算
MUST_NOT_DRAG = [
    ["customers", "products"],
    ["customers", "suppliers"],
    ["customers", "categories"],
    ["customers", "products", "promotions"],
    ["products", "payments"],
]

# 錨點本身就是行為表時，要照樣用得到，不可被權重擋掉
ANCHOR_IS_BEHAVIOURAL = [
    (["browse_logs", "customers"], {"browse_logs", "customers"}),
    (["reviews", "products"], {"products", "reviews"}),
]


def main() -> int:
    get_join_graph()
    log.remove()
    failures: list[str] = []

    for table in BEHAVIOURAL:
        if table_cost(table) <= 1:
            failures.append(f"{table} 的成本應高於預設值，實際 {table_cost(table)}")

    for anchors, required, why in MUST_INCLUDE:
        got = find_join_path(anchors)
        missing = required - got
        if missing:
            failures.append(
                f"{anchors} 少了 {sorted(missing)}（{why}）\n      實際: {sorted(got)}")

    for anchors in MUST_NOT_DRAG:
        got = find_join_path(anchors)
        dragged = (BEHAVIOURAL & got) - set(anchors)
        if dragged:
            failures.append(
                f"{anchors} 被拖進行為表 {sorted(dragged)}\n      實際: {sorted(got)}")

    for anchors, expected in ANCHOR_IS_BEHAVIOURAL:
        got = find_join_path(anchors)
        if got != expected:
            failures.append(
                f"{anchors} 應為 {sorted(expected)}，實際 {sorted(got)}")

    # 決定性：同一組錨點跑很多次必須完全一樣，順序不同也要一樣。
    # MoE 的隨機性已經夠多了，檢索層再不決定性就無法歸因。
    for anchors in ([["customers", "products", "suppliers"]] * 5
                    + [["suppliers", "products", "customers"]] * 5):
        if find_join_path(anchors) != find_join_path(["customers", "products", "suppliers"]):
            failures.append(f"{anchors} 的結果不穩定或與錨點順序有關")
            break

    total = (len(BEHAVIOURAL) + len(MUST_INCLUDE) + len(MUST_NOT_DRAG)
             + len(ANCHOR_IS_BEHAVIOURAL) + 1)
    if failures:
        print(f"schema_graph 測試失敗 {len(failures)} 項 / 共 {total} 項:\n")
        for f in failures:
            print(f"  - {f}")
        return 1

    print(f"schema_graph 測試全數通過（{total} 項）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
