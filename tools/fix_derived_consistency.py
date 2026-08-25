# -*- coding: utf-8 -*-
"""把 profile 表裡「與母表矛盾的衍生欄位」對齊到母表（2026-08-25）。

起因見 ARCHITECTURE §7.9。`check_derived_consistency.py` 掃出 18 欄對不上，
逐項對過建表腳本的意圖之後，只有一部分是真缺陷。這支腳本只動真缺陷，
而且只動「衍生路徑唯一」的那些 —— 路徑不唯一的一律改題目，不改資料。

**判準：母表能不能唯一決定這個值？**

  能（改資料）  return_profiles.is_over_policy_window
                shipment_profiles.attempt_count
                product_profiles.image_count
                promotion_profiles.used_count

  不能（改題目）review_profiles.days_after_delivery
                  reviews 沒有 order_id，評價對到哪一次到貨無法還原：
                  97/134 能連上、其中 10 筆連到 2~3 個不同到貨日，
                  取最早 22 列、取最晚 19 列。母表根本算不出唯一答案，
                  所以 profile 欄位**就是**真相來源，只是 #287 沒說。
                campaign_profiles.clicks / ctr_pct
                  134,911 次 vs campaign_clicks 表 418 列 ——
                  廣告平台的曝光點擊與站內點擊記錄本來就是兩個量，
                  對齊會毀掉 campaign_profiles 的語意。#279 改問句。

  不動（刻意） customer_profiles.order_count / total_spent
                  init_db_ext.seed_customer_profiles 的快照落後 30 天，
                  docstring 寫明「實測 27 位對不上，這是刻意的」，掃描量到 27。

安全性：只有 UPDATE，不建表、不刪列、不重跑 init_db.py。
每次執行前後都算全庫 93 張表的指紋，沒被指名的表**一個位元都不能變**。
可重複執行（把值設成算出來的值，再跑一次是 no-op）。
"""
import argparse
import hashlib
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

# (表, 欄, 說明, UPDATE)。每一條都要能重複執行。
FIXES = [
    ("return_profiles", "is_over_policy_window",
     "requested_at − orders.order_date > policy_window_days。"
     "return→order 一對一（18/18），requested_at 與母表完全相同（18/18），"
     "衍生路徑唯一。原本是常數 0 再由 guarantee() 指定兩列為 1。",
     """UPDATE return_profiles p
        JOIN order_returns orr ON orr.id = p.return_id
        JOIN orders o ON o.id = orr.order_id
        SET p.is_over_policy_window =
            (TIMESTAMPDIFF(DAY, o.order_date, p.requested_at) > p.policy_window_days)"""),

    ("shipment_profiles", "attempt_count",
     "COUNT(delivery_attempts)。shipment_id 是 FK，路徑唯一。"
     "原本是 1 if exception_code='NONE' else randint(2,4)。",
     """UPDATE shipment_profiles p
        JOIN (SELECT s.id sid, COUNT(da.id) n FROM shipments s
              LEFT JOIN delivery_attempts da ON da.shipment_id = s.id
              GROUP BY s.id) d ON d.sid = p.shipment_id
        SET p.attempt_count = d.n"""),

    ("product_profiles", "image_count",
     "COUNT(product_images)。product_id 是 FK，路徑唯一。原本是 randint(1,12)。",
     """UPDATE product_profiles p
        JOIN (SELECT pr.id pid, COUNT(pi.id) n FROM products pr
              LEFT JOIN product_images pi ON pi.product_id = pr.id
              GROUP BY pr.id) d ON d.pid = p.product_id
        SET p.image_count = d.n"""),

    ("promotion_profiles", "used_count",
     "COUNT(order_promotions)。promotion_id 是 FK，路徑唯一。原本是 randint(3,80)。",
     """UPDATE promotion_profiles p
        JOIN (SELECT pr.id pid, COUNT(op.id) n FROM promotions pr
              LEFT JOIN order_promotions op ON op.promotion_id = pr.id
              GROUP BY pr.id) d ON d.pid = p.promotion_id
        SET p.used_count = d.n"""),
]

TOUCHED = {t for t, _c, _w, _s in FIXES}


def fingerprints(conn):
    """每張表一個 SHA：全欄位串起來、排序後雜湊。欄位順序用 information_schema。"""
    db = conn.execute(text("SELECT DATABASE()")).scalar()
    cols = {}
    for t, c in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=:d ORDER BY TABLE_NAME, ORDINAL_POSITION"), {"d": db}):
        cols.setdefault(t, []).append(c)
    fp = {}
    for t, cs in cols.items():
        expr = ",".join(f"COALESCE(CAST(`{c}` AS CHAR),'~')" for c in cs)
        rows = conn.execute(
            text(f"SELECT CONCAT_WS('|',{expr}) FROM `{t}` ORDER BY 1")).scalars().all()
        fp[t] = hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()[:16]
    return fp


def preview(conn):
    """套用前先報「會改幾列、改成什麼」，讓 --dry-run 有東西看。"""
    q = {
        "return_profiles.is_over_policy_window": """
            SELECT SUM(p.is_over_policy_window <>
                       (TIMESTAMPDIFF(DAY,o.order_date,p.requested_at) > p.policy_window_days)),
                   SUM(p.is_over_policy_window),
                   SUM(TIMESTAMPDIFF(DAY,o.order_date,p.requested_at) > p.policy_window_days)
            FROM return_profiles p JOIN order_returns orr ON orr.id=p.return_id
            JOIN orders o ON o.id=orr.order_id""",
        "shipment_profiles.attempt_count": """
            SELECT SUM(p.attempt_count<>d.n), SUM(p.attempt_count), SUM(d.n)
            FROM shipment_profiles p JOIN (SELECT s.id sid, COUNT(da.id) n FROM shipments s
              LEFT JOIN delivery_attempts da ON da.shipment_id=s.id GROUP BY s.id) d
              ON d.sid=p.shipment_id""",
        "product_profiles.image_count": """
            SELECT SUM(p.image_count<>d.n), SUM(p.image_count), SUM(d.n)
            FROM product_profiles p JOIN (SELECT pr.id pid, COUNT(pi.id) n FROM products pr
              LEFT JOIN product_images pi ON pi.product_id=pr.id GROUP BY pr.id) d
              ON d.pid=p.product_id""",
        "promotion_profiles.used_count": """
            SELECT SUM(p.used_count<>d.n), SUM(p.used_count), SUM(d.n)
            FROM promotion_profiles p JOIN (SELECT pr.id pid, COUNT(op.id) n FROM promotions pr
              LEFT JOIN order_promotions op ON op.promotion_id=pr.id GROUP BY pr.id) d
              ON d.pid=p.promotion_id""",
    }
    print(f"{'欄位':46s} {'要改列數':>8s} {'現在合計':>10s} {'對齊後':>10s}")
    for k, s in q.items():
        n, before, after = conn.execute(text(s)).fetchone()
        print(f"  {k:44s} {int(n or 0):>8d} {int(before or 0):>10d} {int(after or 0):>10d}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真的寫入；不加就只預覽")
    ap.add_argument("--fp-out", default=None, help="把套用後的指紋寫到這個檔")
    args = ap.parse_args()
    log.remove()

    eng = get_db_manager(MYSQL_URI).engine
    with eng.connect() as conn:
        before = fingerprints(conn)
        print(f"套用前指紋：{len(before)} 張表\n")
        preview(conn)

    if not args.apply:
        print("\n（--dry-run 模式，什麼都沒寫。加 --apply 才會真的改）")
        return

    print()
    with eng.begin() as conn:
        for t, c, why, sql in FIXES:
            n = conn.execute(text(sql)).rowcount
            print(f"  {t}.{c:26s} 觸及 {n:>4d} 列  ── {why.splitlines()[0]}")

    with eng.connect() as conn:
        after = fingerprints(conn)
        preview(conn)

    changed = {t for t in before if before[t] != after[t]}
    stray = changed - TOUCHED
    print(f"\n指紋變動 {len(changed)} 張：{sorted(changed)}")
    if stray:
        print(f"!! 不該變的表變了：{sorted(stray)} —— 這是嚴重錯誤，回滾並查原因")
        sys.exit(1)
    intact = len(before) - len(changed)
    print(f"其餘 {intact} 張表指紋完全相同 ✓")
    if args.fp_out:
        io.open(args.fp_out, "w", encoding="utf-8").write(
            json.dumps(after, ensure_ascii=False, indent=0))
        print(f"指紋已寫入 {args.fp_out}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
