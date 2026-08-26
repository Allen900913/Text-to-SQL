# -*- coding: utf-8 -*-
"""執行探測：問「每客戶消費金額／訂單張數」的題目，走 profile 快照會不會答出不同的答案？

餘弦只能當粗篩（實測差 0.001 的雜訊一堆，而用已知的那一題去調門檻正是專案禁止的
「調常數」）。**真正的判準是實際跑兩條路看答案一不一樣**（Soma-SQL 的 execution probing）。

每一題手寫「快照路線」的等價 SQL —— 把 `SUM(orders.total_amount)` 換成
`customer_profiles.total_spent`、`COUNT(orders.id)` 換成 `customer_profiles.order_count`，
其餘結構完全不動。答案不同 = 這一題有兩個都說得通的答案，而 GT 只認一個。
"""
import io
import sys

sys.path.insert(0, r"C:\Text-to-SQL")

import yaml
from loguru import logger as log
from sqlalchemy import text

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager

# 快照路線：結構與 GT 相同，只把「從 orders 現算」換成「讀 customer_profiles 的欄位」。
SNAPSHOT = {
5: """SELECT SUM(p.total_spent) AS total FROM customers c
      JOIN customer_profiles p ON p.customer_id = c.id WHERE c.city = '高雄市'""",
30: """WITH per_city AS (SELECT c.city ct, AVG(p.total_spent) a FROM customers c
       JOIN customer_profiles p ON p.customer_id = c.id GROUP BY c.city)
       SELECT ct, a FROM per_city WHERE a = (SELECT MAX(a) FROM per_city)""",
46: "SELECT AVG(total_spent) AS a FROM customer_profiles",
58: """SELECT c.name, p.total_spent AS total FROM customers c
       JOIN customer_profiles p ON p.customer_id = c.id ORDER BY total DESC LIMIT 3""",
60: """WITH t AS (SELECT c.city ct, c.name n, p.total_spent s FROM customers c
       JOIN customer_profiles p ON p.customer_id = c.id),
       m AS (SELECT ct, MAX(s) ms FROM t GROUP BY ct)
       SELECT t.ct, t.n, t.s FROM t JOIN m ON m.ct = t.ct AND m.ms = t.s ORDER BY t.ct, t.n""",
93: """WITH r AS (SELECT total_spent s, ROW_NUMBER() OVER (ORDER BY total_spent) rn,
       COUNT(*) OVER () c FROM customer_profiles)
       SELECT AVG(s) AS median FROM r WHERE rn IN (FLOOR((c+1)/2), CEIL((c+1)/2))""",
94: """WITH ca AS (SELECT c.city ci, AVG(p.total_spent) a FROM customers c
       JOIN customer_profiles p ON p.customer_id = c.id GROUP BY c.city)
       SELECT c.name FROM customers c JOIN customer_profiles p ON p.customer_id = c.id
       JOIN ca ON ca.ci = c.city WHERE p.total_spent > ca.a ORDER BY c.name""",
100: """SELECT c.name FROM customers c JOIN customer_profiles p ON p.customer_id = c.id
        WHERE c.city = '新北市' AND p.total_spent > 50000 ORDER BY c.name""",
4: """SELECT SUM(p.order_count) / COUNT(*) AS avg_orders FROM customers c
      JOIN customer_profiles p ON p.customer_id = c.id WHERE c.city = '新北市'""",
26: """WITH t AS (SELECT c.name n, p.order_count k FROM customers c
       JOIN customer_profiles p ON p.customer_id = c.id)
       SELECT n, k FROM t WHERE k = (SELECT MAX(k) FROM t) ORDER BY n""",
44: """SELECT c.name FROM customers c JOIN customer_profiles p ON p.customer_id = c.id
       WHERE p.order_count = 1 ORDER BY c.name""",
45: """SELECT c.city, SUM(p.order_count) / COUNT(*) AS avg_orders FROM customers c
       JOIN customer_profiles p ON p.customer_id = c.id GROUP BY c.city ORDER BY c.city""",
74: "SELECT customer_id FROM customer_profiles WHERE order_count > 20",
56: """SELECT (SELECT COUNT(*) FROM customer_profiles WHERE order_count > 0) * 100.0
        / (SELECT COUNT(*) FROM customers) AS pct""",
}


def main():
    log.remove()
    gt = {x["id"]: x for x in yaml.safe_load(
        io.open(r"C:\Text-to-SQL\eval_ground_truth.yaml", encoding="utf-8"))}
    same, diff, err = [], [], []
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        print(f"{'題':>5s}  {'GT（從 orders 現算）':38s} {'快照（customer_profiles）':38s} 判定")
        print("-" * 104)
        for qid, alt in SNAPSHOT.items():
            try:
                a = [tuple(r) for r in c.execute(text(gt[qid]["sql"])).fetchall()]
                b = [tuple(r) for r in c.execute(text(alt)).fetchall()]
            except Exception as e:
                err.append((qid, str(e)[:60]))
                print(f"#{qid:<4d}  SQL 失敗 {str(e)[:70]}")
                continue
            ok = sorted(map(str, a)) == sorted(map(str, b))
            (same if ok else diff).append(qid)
            sa = f"{len(a)} 列 {str(a[:1])[:30]}"
            sb = f"{len(b)} 列 {str(b[:1])[:30]}"
            print(f"#{qid:<4d}  {sa:38s} {sb:38s} {'相同' if ok else '★不同'}")
    print("-" * 104)
    print(f"\n兩條路答案相同 {len(same)} 題：{same}")
    print(f"★ 兩條路答案不同 {len(diff)} 題：{diff}")
    if err:
        print(f"SQL 失敗 {len(err)}：{err}")
    print("\n『不同』的每一題都有兩個說得通的答案，而 GT 只認一個 —— "
          "這就是 #94/#100 8/8 → 0/8 的來源。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
