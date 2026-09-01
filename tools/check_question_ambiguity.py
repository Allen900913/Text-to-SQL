# -*- coding: utf-8 -*-
"""閘門第 [10] 項：題目層的多來源歧義 —— GT 沒用、但模型會用的那條路。

**為什麼閘門 [9] 抓不到（2026-08-26，ARCHITECTURE §2.7f 末的「閘門 [10]」小節）**

`check_derived_consistency.py` 檢查「欄位」，而且用 GT 的 SQL 判斷「有沒有題目引用」。
但 `#94`「找出消費金額高於所在城市平均消費的客戶」的 GT 走的是
`SUM(orders.total_amount)` —— **GT 根本沒提 `total_spent`**，閘門 [9] 是綠的。
危險的正是 GT **沒**用、而模型會用的那條路。

> **閘門 [9] 檢查欄位，這一支檢查題目。**

**為什麼判準是「執行」而不是「相似度」**

第一版用餘弦：`cos(問句, 雙來源欄位) >= max(GT 需要的表的最像欄位)`。
實測在 305 題上標出一堆差 0.001~0.003 的雜訊
（「三體這本書總共賣出幾本」vs `prior_ticket_count`）。
而**用已知的那一題去調門檻，正是專案禁止的「調常數」**。

所以改成 Soma-SQL（arXiv 2606.11424）那條路：**實際跑兩條 SQL，比對結果**。
不一致本身就是訊號，不需要門檻。代價是替代路徑要手寫 —— 14 條，可以接受。

**這支腳本量到什麼（2026-08-26 首次執行）**

14 題裡 **11 題兩條路答案不同**。但**這 11 題不是缺陷**：

  · 11 題在 e2e **全部答對**（模型自己走了 orders 那條路）
  · 欄位註解**明說**「每日結算快照，不含最近數日訂單，與即時 SUM(orders.total_amount) 可能不同」

也就是說 schema 自己已經裁決了哪一個算數，而模型讀得懂。
這是 `seed_customer_profiles` 刻意埋的快照陷阱在正常運作，
`#146`／`#147`／`#148` 三題就是專門在問這個落差。

**所以這支腳本的用途是迴歸測試，不是缺陷清單：**
如果哪天這些題開始「兩條路答案相同」，代表快照陷阱死了
（有人把資料對齊了），那三題就失去鑑別力，要有人知道。

零 LLM，純 SQL，不寫任何東西進資料庫。
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

# 快照路線：結構與 GT 相同，只把「從 orders 現算」換成「讀 customer_profiles 的欄位」。
# 這些不是候選答案，是**探針** —— 用來確認「另一條路存在而且答得出不同的東西」。
SNAPSHOT_ROUTE = {
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
           SELECT t.ct, t.n, t.s FROM t JOIN m ON m.ct = t.ct AND m.ms = t.s
           ORDER BY t.ct, t.n""",
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

# 2026-08-26 實測：這幾題兩條路答案**不同**，而 GT 認的是從 orders 現算的那一條。
# 這是快照陷阱在運作，不是缺陷。名單變動要有人看到 —— 少了代表陷阱死了。
EXPECTED_DIVERGENT = {4, 26, 30, 44, 45, 46, 58, 60, 93, 94, 100}


def main():
    log.remove()
    gt = {x["id"]: x for x in yaml.safe_load(
        io.open(os.path.join(_ROOT, "eval_ground_truth.yaml"), encoding="utf-8"))}
    same, diff, err = set(), set(), []
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        print(f"{'題':>5s}  {'GT（從明細現算）':30s} {'另一條路（快照欄位）':30s} 判定")
        print("-" * 90)
        for qid, alt in SNAPSHOT_ROUTE.items():
            try:
                a = [tuple(r) for r in c.execute(text(gt[qid]["sql"])).fetchall()]
                b = [tuple(r) for r in c.execute(text(alt)).fetchall()]
            except Exception as e:
                err.append((qid, str(e)[:70]))
                print(f"#{qid:<4d}  SQL 失敗 {str(e)[:60]}")
                continue
            ok = sorted(map(str, a)) == sorted(map(str, b))
            (same if ok else diff).add(qid)
            print(f"#{qid:<4d}  {f'{len(a)} 列 {str(a[:1])[:22]}':30s} "
                  f"{f'{len(b)} 列 {str(b[:1])[:22]}':30s} {'相同' if ok else '★不同'}")
    print("-" * 90)
    print(f"\n兩條路相同 {len(same)} 題：{sorted(same)}")
    print(f"兩條路不同 {len(diff)} 題：{sorted(diff)}")

    bad = []
    if diff - EXPECTED_DIVERGENT:
        bad.append(f"新增的分歧題：{sorted(diff - EXPECTED_DIVERGENT)} —— "
                   f"有題目新暴露在雙來源歧義下，要逐題判")
    if EXPECTED_DIVERGENT - diff:
        bad.append(f"消失的分歧題：{sorted(EXPECTED_DIVERGENT - diff)} —— "
                   f"快照陷阱可能被誰對齊掉了，#146/#147/#148 會失去鑑別力")
    if err:
        bad.append(f"SQL 失敗 {len(err)} 題：{err}")
    if bad:
        print()
        for b in bad:
            print(f"!! {b}")
        sys.exit(1)
    print(f"\n閘門 [10] 綠燈：分歧名單與 2026-08-26 的實測一致（{len(EXPECTED_DIVERGENT)} 題）。")
    print("提醒：這些**不是缺陷**。11 題在 e2e 全部答對，欄位註解也明說了"
          "「每日結算快照…與即時 SUM(orders.total_amount) 可能不同」——\n"
          "      schema 自己裁決了哪個算數，而模型讀得懂。改掉它們會毀掉快照陷阱。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
