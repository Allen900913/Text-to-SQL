import json
from sqlalchemy import create_engine, text

DB_URI = "mysql+pymysql://root:123456@127.0.0.1:3306/ecommerce_demo"
engine = create_engine(DB_URI)

def run_query(q):
    with engine.connect() as conn:
        res = conn.execute(text(q))
        return [dict(row._mapping) for row in res]

results = {}

# Q0-1
res0_1 = run_query("SELECT COUNT(*) as c FROM customers;")
results["q0_1"] = f"查詢結果顯示，目前資料庫中總共有 {res0_1[0]['c']} 位客戶。"

# Q0-2
res0_2 = run_query("SELECT name, price FROM products WHERE category = '家電' AND price > 5000;")
items = "、".join([f"{r['name']} ({int(r['price']):,}元)" for r in res0_2])
results["q0_2"] = f"家電分類中單價超過 5000 元的商品有：{items}。"

# Q1-1
q1_1 = """
SELECT c.name, SUM(o.total_amount) as total
FROM customers c
JOIN orders o ON c.id = o.customer_id
WHERE c.city = '台北市' AND o.status = 'COMPLETED'
GROUP BY c.id, c.name
ORDER BY total DESC
LIMIT 3;
"""
res1_1 = run_query(q1_1)
top_spenders = "、".join([f"{i+1}. {r['name']} ({int(r['total']):,}元)" for i, r in enumerate(res1_1)])
results["q1_1"] = f"根據查詢結果，居住在台北市的客戶中，累積消費總額最高的前三名是：{top_spenders}。"

# Q1-2
q1_2 = """
SELECT p.category, SUM(oi.quantity) as total_qty
FROM orders o
JOIN order_items oi ON o.id = oi.order_id
JOIN products p ON oi.product_id = p.id
WHERE o.status = 'SHIPPED'
GROUP BY p.category
ORDER BY total_qty DESC
LIMIT 1;
"""
res1_2 = run_query(q1_2)
results["q1_2"] = f"在所有已出貨的訂單中，銷售數量最高的商品分類是「{res1_2[0]['category']}」，總共賣出了 {int(res1_2[0]['total_qty'])} 個。"

# Q2-1
q2_1 = """
SELECT c.name, c.phone, DATE(o.order_date) as odate
FROM customers c
JOIN orders o ON c.id = o.customer_id
JOIN order_items oi ON o.id = oi.order_id
JOIN products p ON oi.product_id = p.id
WHERE p.name = 'MacBook Air'
ORDER BY o.order_date DESC
LIMIT 5;
"""
res2_1 = run_query(q2_1)
buyers = "、".join([f"{i+1}. {r['name']} ({r['phone']}, {r['odate']})" for i, r in enumerate(res2_1)])
results["q2_1"] = f"以下是最近買過 MacBook Air 的前 5 筆客戶紀錄：{buyers}。"

# Q2-2
q2_2 = """
SELECT c.name, COUNT(o.id) as cancel_count
FROM customers c
JOIN orders o ON c.id = o.customer_id
WHERE o.status = 'CANCELLED'
GROUP BY c.id, c.name
ORDER BY cancel_count DESC, c.name ASC;
"""
res2_2 = run_query(q2_2)
cancels = "、".join([f"{r['name']} (取消 {r['cancel_count']} 筆)" for r in res2_2])
results["q2_2"] = f"查詢發現有 {len(res2_2)} 位客戶曾被取消訂單：{cancels}。"

# Q3-1
q3_1_avg = "SELECT AVG(total_amount) as avg_amt FROM orders;"
res3_1_avg = run_query(q3_1_avg)
avg_val = res3_1_avg[0]['avg_amt']

q3_1_max = f"""
SELECT c.name, o.total_amount
FROM orders o
JOIN customers c ON o.customer_id = c.id
WHERE o.total_amount > {avg_val}
ORDER BY o.total_amount DESC
LIMIT 1;
"""
res3_1_max = run_query(q3_1_max)
results["q3_1"] = f"所有訂單的平均消費金額約為 {int(avg_val):,} 元。其中金額最高的一筆訂單是由「{res3_1_max[0]['name']}」所購買，總花費為 {int(res3_1_max[0]['total_amount']):,} 元。"

with open("answers.json", "w", encoding="utf-8") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
