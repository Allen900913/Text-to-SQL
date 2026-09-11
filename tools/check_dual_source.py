# -*- coding: utf-8 -*-
"""閘門 [10b] 雙來源歧義（機械版）—— 同一個量，寬表快照與母表現算不一樣。

為什麼要有這一支，既然已經有 [10]
================================================================
`check_question_ambiguity.py`（閘門 [10]）**不是掃描器**，是 14 條手寫的
替代路徑探針，而且把開發集的路徑寫死在第 101 行、不吃參數。它的職責是
守「快照陷阱有沒有死掉」的迴歸，那個職責它做得很好。

但它的副作用是：**驗收集與兩組驗證集從來沒有被這條軸掃過**。
2026-09-11 風格驗證集 30 題失手裡，最大的一桶（15 題「選錯表」）
有一半是這條軸 ——

    #3036「客人平均總共花了多少錢？」
         GT   AVG(customer_profiles.total_spent)      = 65,814
         系統 AVG(SUM(orders.total_amount) per 客人)  = 85,562
    兩個都對。問句沒說是哪一個。

閘門 [13]／[6]／[14] 都被我寫成吃任何題庫，[10] 比它們早，寫在那個
習慣之前。這一支就是把那條軸補成掃描器。

怎麼判（沿用閘門 [6] 的作法）
================================================================
不用字面規則猜，直接**把另一條路跑一次**：把 GT 碰到的寬表換成一個
「欄位完全一樣、但雙來源欄位改用母表現算」的子查詢，重跑，答案會變
就是歧義。答案不變的不報 —— 那題無論走哪條路都一樣，沒有暴露。

射程（誠實說清楚）
================================================================
**這一支只掃一個方向。** 它要求 GT 自己走寬表，才有東西可以替換：

    [10]  GT 走母表現算 → 拿快照那條路當探針   （手寫，開發集 11 題命中）
    [10b] GT 走寬表快照 → 拿母表現算當探針     （機械，這一支）

反方向沒辦法機械化：要認出「SUM(orders.total_amount) GROUP BY customer_id」
等於「customer_profiles.total_spent」，得在聚合形狀上做模式比對，那很脆。
[10] 的 14 條探針是手寫的，正是因為那個方向寫不出通則。

所以驗收集在 [10b] 綠燈**只代表它沒有「GT 走寬表」的那一類**，
不代表它沒有「GT 走母表、模型走寬表」的那一類 —— 那一半目前仍然沒有偵測器。

另外只涵蓋 RECOMPUTE 裡登記的寬表與欄位。沒登記的欄位不會被掃到，
**綠燈不代表這條軸乾淨，只代表登記過的部分乾淨**。
衍生式取自閘門 [9] `check_derived_consistency.py` 的 CHECKS，
那裡是這個專案對「這一欄怎麼從母表算出來」的權威登記。

一個刻意的差別：[9] 的衍生式帶「截止點」（快照落後 30 天），這裡
**不帶**。因為模型現算的時候不會知道有截止點 —— 要模擬的是模型會走的
那條路，不是資料產生器走的那條路。

用法
    python tools/check_dual_source.py eval/testset_holdout.yaml
    python tools/check_dual_source.py            # 預設掃開發集
"""
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "eval")):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml
from sqlalchemy import text

from eval_score import match_ordered, match_unordered, to_rows
from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager

# 寬表欄位 → 從母表現算的表達式。子查詢裡寬表的別名固定是 p。
RECOMPUTE = {
    "customer_profiles": {
        "order_count":
            "(SELECT COUNT(*) FROM orders o WHERE o.customer_id = p.customer_id)",
        "total_spent":
            "(SELECT COALESCE(SUM(o.total_amount),0) FROM orders o "
            "WHERE o.customer_id = p.customer_id)",
        "avg_order_value":
            "(SELECT AVG(o.total_amount) FROM orders o WHERE o.customer_id = p.customer_id)",
        "max_order_value":
            "(SELECT MAX(o.total_amount) FROM orders o WHERE o.customer_id = p.customer_id)",
        "total_items_bought":
            "(SELECT COALESCE(SUM(oi.quantity),0) FROM orders o "
            "JOIN order_items oi ON oi.order_id = o.id WHERE o.customer_id = p.customer_id)",
        "review_count":
            "(SELECT COUNT(*) FROM reviews r WHERE r.customer_id = p.customer_id)",
        "avg_review_score":
            "(SELECT AVG(r.rating) FROM reviews r WHERE r.customer_id = p.customer_id)",
        "browse_count":
            "(SELECT COUNT(*) FROM browse_logs b WHERE b.customer_id = p.customer_id)",
        "cart_abandon_count":
            "(SELECT COALESCE(SUM(k.is_abandoned),0) FROM carts k "
            "WHERE k.customer_id = p.customer_id)",
        "coupon_used_count":
            "(SELECT COUNT(*) FROM coupon_redemptions cr "
            "WHERE cr.customer_id = p.customer_id)",
        "return_count":
            "(SELECT COUNT(*) FROM orders o JOIN order_returns orr ON orr.order_id = o.id "
            "WHERE o.customer_id = p.customer_id)",
        "first_order_at":
            "(SELECT MIN(o.order_date) FROM orders o WHERE o.customer_id = p.customer_id)",
        "last_order_at":
            "(SELECT MAX(o.order_date) FROM orders o WHERE o.customer_id = p.customer_id)",
        "last_login_at":
            "(SELECT MAX(l.logged_in_at) FROM customer_login_logs l "
            "WHERE l.customer_id = p.customer_id AND l.success = 1)",
        "last_review_at":
            "(SELECT MAX(r.created_at) FROM reviews r WHERE r.customer_id = p.customer_id)",
    },
    "review_profiles": {
        "reply_count":
            "(SELECT COUNT(*) FROM review_replies rr WHERE rr.review_id = p.review_id)",
        "has_merchant_reply":
            "(SELECT COUNT(*) > 0 FROM review_replies rr WHERE rr.review_id = p.review_id)",
    },
    "shipment_profiles": {
        "attempt_count":
            "(SELECT COUNT(*) FROM delivery_attempts da "
            "WHERE da.shipment_id = p.shipment_id)",
    },
    "product_profiles": {
        "image_count":
            "(SELECT COUNT(*) FROM product_images pi WHERE pi.product_id = p.product_id)",
    },
    "promotion_profiles": {
        "used_count":
            "(SELECT COUNT(*) FROM order_promotions op "
            "WHERE op.promotion_id = p.promotion_id)",
    },
    "support_ticket_profiles": {
        "prior_ticket_count":
            "(SELECT COUNT(*) FROM support_tickets t2 JOIN support_tickets t1 "
            "ON t1.id = p.ticket_id AND t2.customer_id = t1.customer_id "
            "AND t2.created_at < t1.created_at)",
    },
}

# 問句已經講明走哪一條路的字樣（「依照實際下單紀錄逐張加總」「根據客戶檔案的快照」）。
SETTLED = ("檔案上", "檔案裡", "快照", "逐張", "逐筆", "實際下單紀錄", "明細",
           "現算", "即時", "登記的", "紀錄上")

# 逐題裁決：問句的**主題**就是那個落差本身。
ADJUDICATED = {
    146: "「根據客戶檔案的消費快照」問句自己指名了快照那條路",
    147: "專門在問快照與現算的落差",
    148: "專門在問快照與現算的落差",
}


def with_recomputed(sql: str, table: str, cols: list, recompute: dict) -> str:
    """把 FROM/JOIN 到的寬表換成「雙來源欄位改用母表現算」的等價子查詢。

    其餘欄位原樣帶出來，所以外層一個字都不用動 —— 跟閘門 [6] 同一招。
    """
    inner = "SELECT " + ", ".join(
        "%s AS `%s`" % (recompute[c], c) if c in recompute else "p.`%s`" % c
        for c in cols) + " FROM `%s` p" % table
    pat = re.compile(
        r"\b(FROM|JOIN)\s+`?%s`?\b(\s+(?!ON\b|WHERE\b|GROUP\b|JOIN\b|LEFT\b|LIMIT\b"
        r"|ORDER\b|HAVING\b|UNION\b)(?:AS\s+)?([A-Za-z_]\w*))?" % re.escape(table), re.I)

    def rep(m):
        return "%s (%s) %s" % (m.group(1), inner, m.group(3) or table)

    out, n = pat.subn(rep, sql)
    return out if n else ""


def scan(entries, db, cols_of):
    hits, adj, n = [], [], 0
    for e in entries:
        sql = e.get("sql")
        if not sql or e.get("expect") == "schema_unsupported":
            continue
        n += 1
        if any(w in e["question"] for w in SETTLED):
            continue
        try:
            gt = to_rows(db.execute_to_dataframe(sql))
        except Exception:
            continue
        m = match_ordered if e.get("ordered") else match_unordered
        for table, recompute in RECOMPUTE.items():
            if not re.search(r"\b(FROM|JOIN)\s+`?%s`?\b" % table, sql, re.I):
                continue
            alt = with_recomputed(sql, table, cols_of[table], recompute)
            if not alt:
                continue
            try:
                arows = to_rows(db.execute_to_dataframe(alt))
            except Exception:
                continue
            if not m(arows, gt):
                rec = (adj if e["id"] in ADJUDICATED else hits)
                rec.append((e["id"], table, len(gt), len(arows)))
                break
    return n, hits, adj


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = args[0] if args else os.path.join(_ROOT, "eval_ground_truth.yaml")
    if not os.path.isabs(path):
        path = os.path.join(_ROOT, path)
    entries = yaml.safe_load(io.open(path, encoding="utf-8"))
    db = get_db_manager(MYSQL_URI)
    with db.engine.connect() as c:
        cols_of = {}
        for t in RECOMPUTE:
            cols_of[t] = [r[0] for r in c.execute(text(
                "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                "ORDER BY ORDINAL_POSITION"), {"t": t})]
    missing = [t for t, cs in cols_of.items() if not cs]
    if missing:
        print("✗ 這些登記的寬表在資料庫裡不存在：%s" % missing)
        return 1

    n, hits, adj = scan(entries, db, cols_of)
    print("題庫：%s" % os.path.relpath(path, _ROOT))
    print("閘門 [10b] 雙來源歧義 —— %d 題可量測，射程 %d 張寬表 / %d 個欄位\n"
          % (n, len(RECOMPUTE), sum(len(v) for v in RECOMPUTE.values())))
    if hits:
        print("命中 %d 題（%.1f%%）—— 問句決定不了走快照還是走母表現算："
              % (len(hits), 100.0 * len(hits) / n))
        for qid, t, a, b in hits:
            print("   #%-6s %s　快照 %d 列 / 現算 %d 列" % (qid, t, a, b))
        print("\n改法：問句講明走哪一條（「依客戶檔案上的累計金額」／「照實際訂單逐張加總」），")
        print("**GT 不動**。反過來改 GT 去迎合系統，就是看著結果挑答案。")
    if adj:
        print("\nℹ️ 已裁決 %d 題（落差本身就是題目的主題）：" % len(adj))
        for qid, t, a, b in adj:
            print("   #%-6s %s" % (qid, ADJUDICATED[qid]))
    if not hits:
        print("閘門 [10b] 綠燈 —— 但只在登記的射程內。沒登記的欄位掃不到。")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
