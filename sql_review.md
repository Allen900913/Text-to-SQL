1. Q: 找出所有姓「陳」的客戶名單
   SQL: SELECT name, email, city FROM customers WHERE name LIKE '陳%'
2. Q: 總共賣出幾台「空氣清淨機」？
   SQL: SELECT SUM(oi.quantity) AS total_sold FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id WHERE p.name = '空氣清淨機'
3. Q: 在台北市的客戶，總共下了多少張訂單？
   SQL: SELECT COUNT(o.id) AS total_orders FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE c.city = '台北市'
4. Q: 哪一個商品的總營收最高？總共賺了多少錢？
   SQL: SELECT p.name, SUM(oi.quantity * oi.unit_price) AS total_revenue FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id GROUP BY p.id, p.name ORDER BY total_revenue DESC LIMIT 1
5. Q: 有多少張訂單的狀態是待處理 (PENDING)？
   SQL: SELECT COUNT(*) AS pending_count FROM orders WHERE status = 'PENDING'
6. Q: 找出從未下過任何訂單的客戶姓名
   SQL: SELECT c.name FROM customers AS c LEFT JOIN orders AS o ON c.id = o.customer_id WHERE o.id IS NULL ORDER BY c.name
7. Q: 單筆訂單總金額超過 5 萬元的訂單，都是哪些客戶買的？
   SQL: SELECT c.name, o.id AS order_id, o.total_amount, o.order_date FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE o.total_amount > 50000 ORDER BY o.total_amount DESC
8. Q: 最晚註冊的客戶是誰？他註冊的日期是哪天？
   SQL: SELECT name, created_at FROM customers ORDER BY created_at DESC LIMIT 1
9. Q: 商品庫存量低於 10 的商品有哪些？
   SQL: SELECT name, category, stock FROM products WHERE stock < 10 ORDER BY stock ASC
10. Q: 平均每張訂單買了幾項不同的商品？
   SQL: SELECT AVG(item_count) AS avg_items_per_order FROM (SELECT order_id, COUNT(DISTINCT product_id) AS item_count FROM order_items GROUP BY order_id) AS order_summary
11. Q: 請先幫我計算出所有訂單的『平均消費金額』大約是多少？接著，在所有『高於平均值』的訂單中，金額最高的那一筆是哪位客戶買的？花了多少錢？
   SQL: SELECT (SELECT AVG(total_amount) FROM orders) AS avg_amount, c.name, o.total_amount, o.order_date FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE o.total_amount > (SELECT AVG(total_amount) FROM orders) ORDER BY o.total_amount DESC LIMIT 1
12. Q: 找出所有姓「林」的客戶名單
   SQL: SELECT name, email, city FROM customers WHERE name LIKE '林%' ORDER BY name
13. Q: 總共賣出幾台「iPhone 15」？
   SQL: SELECT SUM(oi.quantity) AS total_sold FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id WHERE p.name = 'iPhone 15'
14. Q: 在高雄市的客戶，總共下了多少張訂單？
   SQL: SELECT COUNT(o.id) AS total_orders FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE c.city = '高雄市'
15. Q: 哪一個商品的總營收最低？
   SQL: SELECT p.name, SUM(oi.quantity * oi.unit_price) AS total_revenue FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id GROUP BY p.id, p.name ORDER BY total_revenue ASC LIMIT 1
16. Q: 有多少張訂單的狀態是已出貨 (SHIPPED)？
   SQL: SELECT COUNT(*) AS shipped_count FROM orders WHERE status = 'SHIPPED'
17. Q: 找出沒有任何購買紀錄的客戶
   SQL: SELECT c.name FROM customers AS c LEFT JOIN orders AS o ON c.id = o.customer_id WHERE o.id IS NULL
18. Q: 單筆訂單總金額低於 1000 元的訂單，都是哪些客戶買的？
   SQL: SELECT c.name, o.id AS order_id, o.total_amount, o.order_date FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE o.total_amount < 1000 ORDER BY o.total_amount ASC
19. Q: 最早註冊的客戶是誰？
   SQL: SELECT name, created_at FROM customers ORDER BY created_at ASC LIMIT 1
20. Q: 庫存量大於 100 的商品有哪些？
   SQL: SELECT name, category, stock FROM products WHERE stock > 100 ORDER BY stock ASC
21. Q: 列出所有客戶的電子郵件信箱
   SQL: SELECT email FROM customers ORDER BY email ASC
22. Q: 有沒有人的電話號碼是 09 開頭的？請列出他們的姓名
   SQL: SELECT name FROM customers WHERE phone LIKE '09%'
23. Q: 列出所有在台中市的客戶姓名和註冊時間
   SQL: SELECT c.name, c.created_at FROM customers AS c WHERE c.city = '台中市'
24. Q: 請告訴我商品分類有哪些？
   SQL: SELECT category FROM products GROUP BY category
25. Q: 哪些商品的單價超過 20000 元？
   SQL: SELECT name, price FROM products WHERE price > 20000
26. Q: 請列出商品描述中包含『防水』的商品名稱
   SQL: SELECT p.name FROM products AS p WHERE description LIKE '%防水%'
27. Q: 有沒有庫存剛剛好等於 0 的商品？
   SQL: SELECT name, category, stock FROM products WHERE stock = 0
28. Q: 請依據註冊時間由新到舊列出前五名客戶
   SQL: SELECT c.name, c.created_at FROM customers AS c ORDER BY c.created_at DESC LIMIT 5
29. Q: 找出所有取消 (CANCELLED) 的訂單
   SQL: SELECT id, customer_id, order_date, total_amount, status FROM orders WHERE status = 'CANCELLED'
30. Q: 列出 2025 年以前註冊的客戶
   SQL: SELECT name, email, created_at FROM customers WHERE YEAR(created_at) < 2025 ORDER BY created_at ASC
31. Q: 我們的客戶總共有幾位？
   SQL: SELECT COUNT(DISTINCT o.customer_id) AS total_customers FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE NOT c.id IS NULL
32. Q: 商品總共有多少項？
   SQL: SELECT COUNT(*) AS total_items FROM products
33. Q: 所有商品的平均價格是多少？
   SQL: SELECT AVG(price) AS avg_price FROM products
34. Q: 最便宜的商品價格是多少？
   SQL: SELECT MIN(price) AS cheapest_price FROM products
35. Q: 所有庫存加起來總共有幾件？
   SQL: SELECT SUM(stock) AS total_stock FROM products
36. Q: 台北市有多少位客戶？
   SQL: SELECT COUNT(*) AS total_customers FROM customers WHERE city = '台北市'
37. Q: 所有訂單的平均金額是多少？
   SQL: SELECT AVG(total_amount) AS avg_amount FROM orders
38. Q: 總共有多少筆訂單？
   SQL: SELECT COUNT(o.id) AS total_orders FROM orders AS o
39. Q: 狀態是完成 (COMPLETED) 的訂單總共有幾筆？
   SQL: SELECT COUNT(*) AS completed_count FROM orders WHERE status = 'COMPLETED'
40. Q: 今年 (2026年) 成立的訂單有幾筆？
   SQL: SELECT COUNT(o.id) AS total_orders FROM orders AS o WHERE o.order_date BETWEEN '2026-01-01' AND '2026-12-31'
41. Q: 單價最高的商品是多少錢？
   SQL: SELECT MAX(unit_price) AS highest_price FROM order_items JOIN products AS p ON order_items.product_id = p.id
42. Q: 請幫我統計每個城市的客戶數量
   SQL: SELECT c.city, COUNT(c.id) AS customer_count FROM customers AS c GROUP BY c.city ORDER BY customer_count DESC
43. Q: 請幫我統計每個商品分類的商品數量
   SQL: SELECT category, COUNT(id) AS product_count FROM products GROUP BY category ORDER BY product_count DESC
44. Q: 每種訂單狀態各有幾筆訂單？
   SQL: SELECT status, COUNT(*) AS order_count FROM orders GROUP BY status
45. Q: 找出客戶數最多的城市是哪一個？
   SQL: SELECT c.city, COUNT(o.id) AS total_customers FROM customers AS c JOIN orders AS o ON c.id = o.customer_id GROUP BY c.city ORDER BY total_customers DESC LIMIT 1
46. Q: 哪位客戶下了編號 1 號的訂單？
   SQL: SELECT c.name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id WHERE o.id = 1
47. Q: 列出所有下過訂單的客戶姓名
   SQL: SELECT c.name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id
48. Q: 找出訂單金額最高的那張訂單是誰買的？
   SQL: SELECT c.name, o.total_amount, o.order_date FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE o.total_amount = (SELECT MAX(total_amount) FROM orders)
49. Q: 找出 2026 年 5 月下訂單的所有客戶姓名
   SQL: SELECT c.name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id WHERE YEAR(o.order_date) = 2026 AND MONTH(o.order_date) = 5 ORDER BY c.name
50. Q: 找出有買過東西的台中市客戶
   SQL: SELECT c.name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id JOIN order_items AS oi ON o.id = oi.order_id WHERE c.city = '台中市' AND oi.quantity > 0
51. Q: 請列出所有『已付款』(PAID) 的訂單是由哪些客戶購買的
   SQL: SELECT c.name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id WHERE o.status = 'PAID'
52. Q: 哪一位客戶的累計消費總額最高？
   SQL: SELECT c.name, SUM(o.total_amount) AS total_spent FROM customers AS c JOIN orders AS o ON c.id = o.customer_id GROUP BY c.name ORDER BY total_spent DESC LIMIT 1
53. Q: 請列出每位客戶的累計消費總金額
   SQL: SELECT c.name, SUM(o.total_amount) AS total_spent FROM customers AS c JOIN orders AS o ON c.id = o.customer_id GROUP BY c.id, c.name ORDER BY total_spent DESC
54. Q: 有沒有客戶的累計消費總額超過 10 萬元？請列出姓名
   SQL: SELECT c.name FROM customers AS c JOIN (SELECT customer_id, SUM(total_amount) AS total_spent FROM orders GROUP BY customer_id) AS o ON c.id = o.customer_id WHERE o.total_spent > 100000 ORDER BY o.total_spent DESC
55. Q: 計算所有台北市客戶的總消費金額
   SQL: SELECT SUM(o.total_amount) AS total_spent FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE c.city = '台北市'
56. Q: 訂單明細編號 5 買了什麼商品？
   SQL: SELECT p.name FROM products AS p JOIN order_items AS oi ON p.id = oi.product_id WHERE oi.id = 5
57. Q: 『MacBook Air』這個商品總共出現在幾張訂單明細中？
   SQL: SELECT COUNT(DISTINCT oi.order_id) AS total_orders FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id WHERE p.name = 'MacBook Air'
58. Q: 哪一個商品賣出的總數量最多？
   SQL: SELECT p.name, SUM(oi.quantity) AS total_sold FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id GROUP BY p.id, p.name ORDER BY total_sold DESC LIMIT 1
59. Q: 列出所有『手機』分類商品賣出的總數量
   SQL: SELECT SUM(oi.quantity) AS total_sold FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id WHERE p.category = '手機'
60. Q: 請列出每種商品分類的總銷量
   SQL: SELECT p.category, SUM(oi.quantity) AS total_sold FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id GROUP BY p.category
61. Q: 平均來說，每次購買『吹風機』時，大家都會買幾台？
   SQL: SELECT AVG(oi.quantity) AS avg_blowers_per_order FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id WHERE p.name = '吹風機'
62. Q: 訂單編號 10 總共買了幾項不同的商品？
   SQL: SELECT COUNT(DISTINCT oi.product_id) AS item_count FROM order_items AS oi JOIN orders AS o ON oi.order_id = o.id WHERE o.id = 10
63. Q: 訂單編號 3 的明細總金額加起來是多少？
   SQL: SELECT SUM(oi.quantity * oi.unit_price) AS total_amount FROM order_items AS oi WHERE oi.order_id = 3
64. Q: 找出包含超過 5 項不同商品的訂單編號
   SQL: SELECT o.id FROM orders AS o JOIN (SELECT order_id, COUNT(DISTINCT product_id) AS item_count FROM order_items GROUP BY order_id) AS order_summary ON o.id = order_summary.order_id WHERE item_count > 5
65. Q: 請列出每一張訂單分別買了多少件商品（quantity 的總和）
   SQL: SELECT o.id, SUM(oi.quantity) AS total_quantity FROM orders AS o JOIN order_items AS oi ON o.id = oi.order_id GROUP BY o.id
66. Q: 請問『筆記型電腦』(Laptop)這個類別底下，總共有幾種不同的商品？
   SQL: SELECT COUNT(DISTINCT p.id) AS distinct_laptops FROM products AS p WHERE p.category = 'Laptop'
67. Q: 幫我列出單價最貴的前三名商品名稱和價格。
   SQL: SELECT name, price FROM products ORDER BY price DESC LIMIT 3
68. Q: 在 2026 年 5 月份的所有訂單中，總共創造了多少營業額？
   SQL: SELECT SUM(oi.quantity * oi.unit_price) AS total_revenue FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id JOIN orders AS o ON oi.order_id = o.id WHERE YEAR(o.order_date) = 2026 AND MONTH(o.order_date) = 5
69. Q: 請找出狀態是『已出貨』(SHIPPED) 的訂單中，平均每筆訂單的總金額大約是多少？
   SQL: SELECT AVG(o.total_amount) AS avg_amount FROM orders AS o WHERE o.status = 'SHIPPED'
70. Q: 誰買過『iPad Pro』？請列出客戶姓名
   SQL: SELECT c.name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id JOIN order_items AS oi ON o.id = oi.order_id JOIN products AS p ON oi.product_id = p.id WHERE p.name = 'iPad Pro'
71. Q: 住在高雄市的客戶總共買了幾台空氣清淨機？
   SQL: SELECT SUM(oi.quantity) AS total_air_purifiers FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id JOIN orders AS o ON oi.order_id = o.id JOIN customers AS c ON o.customer_id = c.id WHERE c.city = '高雄市' AND p.name = '空氣清淨機'
72. Q: 請列出各個城市購買『手機』分類商品的總數量
   SQL: SELECT c.city, SUM(oi.quantity) AS total_handsets FROM customers AS c JOIN orders AS o ON c.id = o.customer_id JOIN order_items AS oi ON o.id = oi.order_id JOIN products AS p ON oi.product_id = p.id WHERE p.category = '手機' GROUP BY c.city
73. Q: 找出總消費金額最低（但有消費過）的客戶姓名和他的電子郵件
   SQL: SELECT c.name, c.email FROM customers AS c JOIN orders AS o ON c.id = o.customer_id WHERE (SELECT SUM(total_amount) FROM orders WHERE customer_id = c.id) = (SELECT MIN(total_amount) FROM orders)
74. Q: 哪些客戶從來沒買過『筆記型電腦』類別的商品？
   SQL: SELECT c.name FROM customers AS c LEFT JOIN order_items AS oi ON c.id = oi.order_id LEFT JOIN products AS p ON oi.product_id = p.id WHERE p.category IS NULL OR p.category <> '筆記型電腦' ORDER BY c.name
75. Q: 找出同時買過『手機』跟『平板』的客戶
   SQL: SELECT c.name FROM customers AS c JOIN order_items AS oi1 ON c.id = oi1.order_id JOIN products AS p1 ON oi1.product_id = p1.id JOIN order_items AS oi2 ON c.id = oi2.order_id JOIN products AS p2 ON oi2.product_id = p2.id WHERE p1.name = '手機' AND p2.name = '平板' GROUP BY c.name
76. Q: 請列出 2026 年第一季 (1月到3月) 最暢銷的商品名稱
   SQL: SELECT p.name FROM products AS p JOIN order_items AS oi ON p.id = oi.product_id JOIN orders AS o ON oi.order_id = o.id WHERE o.order_date >= '2026-01-01' AND o.order_date <= '2026-03-31' GROUP BY p.name ORDER BY SUM(oi.quantity) DESC LIMIT 1
77. Q: 有沒有任何一張訂單同時包含 3 種以上的商品？
   SQL: SELECT COUNT(DISTINCT o.id) AS orders_with_multiple_items FROM orders AS o JOIN (SELECT order_id, COUNT(DISTINCT product_id) AS item_count FROM order_items GROUP BY order_id) AS order_summary ON o.id = order_summary.order_id WHERE order_summary.item_count > 3
78. Q: 請問所有姓『李』的客戶，總共貢獻了多少營業額？
   SQL: SELECT SUM(oi.quantity * oi.unit_price) AS total_revenue, c.name AS customer_name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id JOIN order_items AS oi ON o.id = oi.order_id WHERE c.name LIKE '李%' GROUP BY c.name
79. Q: 請列出每個月的總營業額走勢
   SQL: SELECT YEAR(o.order_date) AS year, MONTH(o.order_date) AS month, SUM(oi.quantity * oi.unit_price) AS total_revenue FROM order_items AS oi JOIN orders AS o ON oi.order_id = o.id GROUP BY YEAR(o.order_date), MONTH(o.order_date)
80. Q: 找出客單價（每張訂單平均消費）最高的城市
   SQL: SELECT c.city, AVG(o.total_amount) AS avg_amount FROM orders AS o JOIN customers AS c ON o.customer_id = c.id GROUP BY c.city ORDER BY avg_amount DESC LIMIT 1
81. Q: 哪些商品從來沒有被賣出過？
   SQL: SELECT p.name FROM products AS p LEFT JOIN order_items AS oi ON p.id = oi.product_id WHERE oi.id IS NULL
82. Q: 列出從未被取消過的商品名單
   SQL: SELECT p.name FROM products AS p LEFT JOIN order_items AS oi ON p.id = oi.product_id LEFT JOIN orders AS o ON oi.order_id = o.id WHERE o.id IS NULL
83. Q: 有沒有哪位客戶的每一張訂單都超過 2 萬元？
   SQL: SELECT c.name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id WHERE o.total_amount > 20000
84. Q: 請列出各個商品分類的總營收，並由高到低排序
   SQL: SELECT p.category, SUM(oi.quantity * oi.unit_price) AS total_revenue FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id GROUP BY p.category ORDER BY total_revenue DESC
85. Q: 總營收最高的商品分類是哪一個？
   SQL: SELECT category, SUM(oi.quantity * oi.unit_price) AS total_revenue FROM order_items AS oi JOIN products AS p ON oi.product_id = p.id GROUP BY p.category ORDER BY total_revenue DESC LIMIT 1
86. Q: 請找出購買次數最頻繁（訂單數最多）的客戶姓名
   SQL: SELECT c.name, COUNT(o.id) AS order_count FROM customers AS c JOIN orders AS o ON c.id = o.customer_id GROUP BY c.id, c.name ORDER BY order_count DESC LIMIT 1
87. Q: 誰是本月的大客戶？（本月消費總額最高的客戶）
   SQL: SELECT c.name, SUM(o.total_amount) AS total_spent FROM orders AS o JOIN customers AS c ON o.customer_id = c.id WHERE o.order_date <= (SELECT LAST_DAY(DATE_FORMAT(CURRENT_DATE, '%Y-%m-00'))) GROUP BY c.id, c.name ORDER BY total_spent DESC LIMIT 1
88. Q: 找出那些註冊不到一個月就下單的客戶名單
   SQL: SELECT c.name, c.email, c.city FROM customers AS c JOIN orders AS o ON c.id = o.customer_id WHERE c.created_at + INTERVAL '1' MONTH < o.order_date ORDER BY c.name
89. Q: 請問『已退款』或『已取消』的訂單總金額加起來是多少？
   SQL: SELECT SUM(total_amount) AS refunded_and_cancelled_amount FROM orders WHERE status IN ('REFUNDED', 'CANCELLED')
90. Q: 列出所有買過跟『王大明』一樣商品的客戶
   SQL: SELECT c.name, o.id AS order_id, o.total_amount, o.order_date FROM orders AS o JOIN customers AS c ON o.customer_id = c.id JOIN order_items AS oi ON o.id = oi.order_id JOIN products AS p ON oi.product_id = p.id WHERE p.name = (SELECT name FROM products WHERE name = '王大明') GROUP BY c.name, o.id, o.total_amount, o.order_date HAVING COUNT(DISTINCT p.id) > 1
91. Q: 請問我們公司目前的總庫存價值（庫存數量乘上單價）是多少？
   SQL: SELECT SUM(p.stock * p.price) AS total_inventory_value FROM products AS p
92. Q: 有沒有哪個城市的客戶完全沒有下過單？
   SQL: SELECT c.city FROM customers AS c LEFT JOIN orders AS o ON c.id = o.customer_id WHERE o.id IS NULL
93. Q: 請問在週末下的訂單數量多，還是平日下的訂單數量多？
   SQL: SELECT CASE WHEN DAYOFWEEK(o.order_date) BETWEEN 1 AND 5 THEN '平日' ELSE '週末' END AS order_day, COUNT(*) AS order_count FROM orders AS o GROUP BY CASE WHEN DAYOFWEEK(o.order_date) BETWEEN 1 AND 5 THEN '平日' ELSE '週末' END
94. Q: 請列出每次購物（單筆訂單）購買數量超過 10 件的訂單資訊
   SQL: SELECT o.id, SUM(oi.quantity) AS total_quantity, o.total_amount, c.name FROM orders AS o JOIN customers AS c ON o.customer_id = c.id JOIN order_items AS oi ON o.id = oi.order_id GROUP BY o.id, c.name HAVING SUM(oi.quantity) > 10
95. Q: 請問『電視』這個商品，大部分都是被哪個城市的客戶買走的？
   SQL: SELECT c.city, COUNT(o.id) AS total_orders FROM orders AS o JOIN customers AS c ON o.customer_id = c.id JOIN order_items AS oi ON o.id = oi.order_id JOIN products AS p ON oi.product_id = p.id WHERE p.name = '電視' GROUP BY c.city ORDER BY total_orders DESC LIMIT 1
96. Q: 列出那些雖然有註冊，但已經超過一年沒下單的客戶
   SQL: SELECT c.name FROM customers AS c JOIN orders AS o ON c.id = o.customer_id WHERE o.order_date < DATE_SUB(CURRENT_DATE, INTERVAL '1' YEAR) GROUP BY c.id HAVING COUNT(o.id) = 1
