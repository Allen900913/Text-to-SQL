# -*- coding: utf-8 -*-
"""閘門第 [9] 項：profile 表的衍生欄位，與母表算出來的值對得上嗎？

**為什麼要有這一項（2026-08-25，ARCHITECTURE §7.9）**

`#287` 穩定 0/8，前七輪都當成「模型選錯表」在修註解。實際上是
`review_profiles.days_after_delivery` 與「從 reviews / shipments 算」在 97 筆
可對照的資料裡只有 1 筆吻合 —— 模型走了一條在正規化 schema 下完全合理的路，
而 schema 裡沒有任何東西說那條路是假的。**這種題目加再多註解都不會過。**

同樣的形狀在 `#279`（點擊率兩條路指到不同活動）、`#308`（超期退貨 2 列 vs 4 列）
也各中一次。五題穩定錯裡有三題根本不是選表問題。

這不是本專案獨有的坑：AmbiQT（EMNLP 2023）把「加入聚合欄位，與 GROUP BY 現算並存」
當成**刻意的歧義注入手法**；CIDR '26 那篇量到 BIRD mini-dev 有 32% 的題目
帶標註錯誤（金融領域 49%），其中一類就是 T 本身有歧義。

**判準：冗餘可以，矛盾不行。**
兩張表記同一件事、值相同 = 好的檢索測試（93 張表的設計目的就是這個）。
兩張表記同一件事、值不同 = 壞掉的 benchmark，因為「對」沒有定義。

**燈號**
  紅（exit 1）不一致 + 有 GT 引用 + 沒宣告 —— 有題目正在量一個沒有唯一答案的東西
  黃（exit 0）不一致 + 沒有 GT 引用 + 沒宣告 —— 地雷，下次配題問到就會變紅燈
  快          customer_profiles 的快照族：比的是 anchor − 30 天的截止值，不是即時值
  綠          一致，或已在 DECLARED 裡寫明理由

**2026-08-26：快照族從「宣告」升級成「檢查」**

`order_count` / `total_spent` 原本寫在 DECLARED 裡（宣告成不檢查），理由是
「每日結算快照，與即時值本來就不同」。那句話是真的，但它讓兩件事分不開：
**「刻意落後 30 天」與「根本沒有在算」看起來一模一樣。**
實測這兩欄剛好等於 anchor − 30 天的截止值（50/50 全中），所以截止點是可驗證的，
只是從來沒有人驗。改成用截止點檢查之後：

  宣告  只能說「這兩個量本來就不同」
  檢查  能說「它等於它該等於的那個量」—— 而且下次有人動到快照邏輯會當場報錯

同一批的 total_items_bought / return_count / coupon_used_count / cart_abandon_count
欄位註解也寫著「（快照）」，值卻是 R.randint(...)。它們被對齊到**同一個截止點**，
不是對齊到即時值 —— 對齊到即時值會讓同一張表一半欄位是快照、一半是即時。

純 SQL，零 LLM，不寫任何東西進資料庫。
用法：`.venv/Scripts/python.exe tools/check_derived_consistency.py`
"""
import io
import os
import re
import sys
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.data_anchor import DATA_ANCHOR_DATETIME  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

# customer_profiles 的快照截止點，與 init_db_ext.seed_customer_profiles 同源。
SNAPSHOT_LAG_DAYS = 30
CUTOFF = DATA_ANCHOR_DATETIME - timedelta(days=SNAPSHOT_LAG_DAYS)

# 用截止點檢查的欄位。它們**刻意**與即時值不同，而那個差距本身是 #146/#147/#148
# 的鑑別力來源 —— 所以下面另外印一段「即時 vs 快照的差距」，差距歸零要有人知道。
# 已經有腳本負責把它們算出來的欄位（tools/fix_derived_consistency.py 的 FIXES，
# 以及 add_wide_tables*.py 的 derive_from_parents）。
#
# **這些欄位不再是黃燈候選：不一致就是紅燈，不管有沒有題目引用。**
# 黃燈的語意是「還沒有人負責這一欄」，而這些欄位已經有人負責了 ——
# 它們對不上只有一個意思：**那一步沒有跑**（重建資料庫之後忘了跑對齊）。
# 這正是 §8 ① 那個形狀（寫了沒接到產生鏈），差別在這次有東西會喊。
DERIVED = {
    "customer_profiles.total_items_bought",
    "customer_profiles.return_count",
    "customer_profiles.coupon_used_count",
    "customer_profiles.cart_abandon_count",
    "customer_profiles.last_login_at",
    "customer_profiles.last_active_at",
    "review_profiles.reviewer_review_count",
    "review_profiles.reply_count",
    "review_profiles.has_merchant_reply",
    "support_ticket_profiles.prior_ticket_count",
    "return_profiles.is_over_policy_window",
    "shipment_profiles.attempt_count",
    "product_profiles.image_count",
    "promotion_profiles.used_count",
}

SNAPSHOT = {
    "customer_profiles.order_count",
    "customer_profiles.total_spent",
    "customer_profiles.total_items_bought",
    "customer_profiles.return_count",
    "customer_profiles.coupon_used_count",
    "customer_profiles.cart_abandon_count",
}

# 已宣告的刻意不一致。要進這份名單，理由必須是「這兩個量本來就不同」，
# 不能是「對齊起來很麻煩」。沒宣告的不一致一律當缺陷處理。
DECLARED = {
    # customer_profiles.order_count / total_spent 已於 2026-08-26 從這裡移出 ——
    # 它們現在用截止點**檢查**（見 SNAPSHOT），不再是宣告的例外。
    "review_profiles.days_after_delivery":
        "reviews 沒有 order_id，評價對到哪一次到貨無法還原 —— 134 則裡 97 則連得上，"
        "其中 10 則連到 2~3 個不同到貨日（取最早 22 列、取最晚 19 列）。"
        "母表算不出唯一答案，所以這一欄就是真相來源。#287 的問句已收窄成"
        "「評價內容檔案上登記的『到貨後天數』」。",
    "campaign_profiles.ctr_pct":
        "campaign_profiles 的 impressions/clicks 是廣告平台尺度（clicks 合計 134,911），"
        "campaign_clicks 表是站內點擊記錄（418 列），兩者本來就是不同的量。"
        "這是指標定義衝突不是資料漂移，對齊會毀掉語意。#279 的問句已收窄成"
        "「行銷活動檔案上點擊率最高」。",
}

# (欄位, 衍生式的白話, SQL)。SQL 一律回 (一致數, 可對照數, profile 側合計, 衍生側合計)。
# 只收「衍生式明確、不需要猜」的欄位 —— ctr_pct 那種沒有原始素材的算不出來，
# 也就不可能不一致、不可能誤導模型；列進來只會製造假紅燈。
CHECKS = [
    # ---- customer_profiles：素材最齊，欄位最多（57 欄）----
    ("customer_profiles.order_count", "COUNT(orders WHERE order_date<=截止點)", """
     SELECT SUM(p.order_count=d.n), COUNT(*), SUM(p.order_count), SUM(d.n)
     FROM customer_profiles p JOIN (SELECT c.id cid,
       COALESCE(SUM(o.order_date<=:cut),0) n FROM customers c
       LEFT JOIN orders o ON o.customer_id=c.id GROUP BY c.id) d ON d.cid=p.customer_id
     WHERE p.order_count IS NOT NULL"""),
    ("customer_profiles.total_spent", "SUM(orders.total_amount WHERE order_date<=截止點)", """
     SELECT SUM(ABS(p.total_spent-d.s)<0.01), COUNT(*), ROUND(SUM(p.total_spent)), ROUND(SUM(d.s))
     FROM customer_profiles p JOIN (SELECT c.id cid,
       COALESCE(SUM(CASE WHEN o.order_date<=:cut THEN o.total_amount END),0) s
       FROM customers c LEFT JOIN orders o ON o.customer_id=c.id GROUP BY c.id) d
       ON d.cid=p.customer_id WHERE p.total_spent IS NOT NULL"""),
    ("customer_profiles.first_order_at", "MIN(orders.order_date)", """
     SELECT SUM(DATE(p.first_order_at)=DATE(d.m)), COUNT(*), NULL, NULL
     FROM customer_profiles p JOIN (SELECT customer_id cid, MIN(order_date) m
       FROM orders GROUP BY customer_id) d ON d.cid=p.customer_id
     WHERE p.first_order_at IS NOT NULL"""),
    ("customer_profiles.last_order_at", "MAX(orders.order_date)", """
     SELECT SUM(DATE(p.last_order_at)=DATE(d.m)), COUNT(*), NULL, NULL
     FROM customer_profiles p JOIN (SELECT customer_id cid, MAX(order_date) m
       FROM orders GROUP BY customer_id) d ON d.cid=p.customer_id
     WHERE p.last_order_at IS NOT NULL"""),
    ("customer_profiles.review_count", "COUNT(reviews)", """
     SELECT SUM(p.review_count=d.n), COUNT(*), SUM(p.review_count), SUM(d.n)
     FROM customer_profiles p JOIN (SELECT c.id cid, COUNT(r.id) n FROM customers c
       LEFT JOIN reviews r ON r.customer_id=c.id GROUP BY c.id) d ON d.cid=p.customer_id
     WHERE p.review_count IS NOT NULL"""),
    ("customer_profiles.avg_review_score", "AVG(reviews.rating)", """
     SELECT SUM(ABS(p.avg_review_score-d.a)<0.05), COUNT(*), NULL, NULL
     FROM customer_profiles p JOIN (SELECT customer_id cid, AVG(rating) a FROM reviews
       GROUP BY customer_id) d ON d.cid=p.customer_id WHERE p.avg_review_score IS NOT NULL"""),
    ("customer_profiles.return_count", "COUNT(order_returns WHERE requested_at<=截止點)", """
     SELECT SUM(p.return_count=d.n), COUNT(*), SUM(p.return_count), SUM(d.n)
     FROM customer_profiles p JOIN (SELECT c.id cid,
       COALESCE(SUM(orr.requested_at<=:cut),0) n FROM customers c
       LEFT JOIN orders o ON o.customer_id=c.id
       LEFT JOIN order_returns orr ON orr.order_id=o.id GROUP BY c.id) d
       ON d.cid=p.customer_id WHERE p.return_count IS NOT NULL"""),
    ("customer_profiles.total_items_bought", "SUM(order_items.quantity WHERE order_date<=截止點)", """
     SELECT SUM(p.total_items_bought=d.n), COUNT(*), SUM(p.total_items_bought), SUM(d.n)
     FROM customer_profiles p JOIN (SELECT c.id cid,
       COALESCE(SUM(CASE WHEN o.order_date<=:cut THEN oi.quantity END),0) n
       FROM customers c LEFT JOIN orders o ON o.customer_id=c.id
       LEFT JOIN order_items oi ON oi.order_id=o.id GROUP BY c.id) d
       ON d.cid=p.customer_id WHERE p.total_items_bought IS NOT NULL"""),
    ("customer_profiles.browse_count", "COUNT(browse_logs)", """
     SELECT SUM(p.browse_count=d.n), COUNT(*), SUM(p.browse_count), SUM(d.n)
     FROM customer_profiles p JOIN (SELECT c.id cid, COUNT(b.id) n FROM customers c
       LEFT JOIN browse_logs b ON b.customer_id=c.id GROUP BY c.id) d
       ON d.cid=p.customer_id WHERE p.browse_count IS NOT NULL"""),
    # 只算成功的登入：失敗的嘗試不是「登入過」。50 位客戶都有成功紀錄，不會產生 NULL。
    ("customer_profiles.last_login_at", "MAX(customer_login_logs WHERE success=1)", """
     SELECT SUM(DATE(p.last_login_at)=DATE(d.m)), COUNT(*), NULL, NULL
     FROM customer_profiles p JOIN (SELECT customer_id cid, MAX(logged_in_at) m
       FROM customer_login_logs WHERE success=1 GROUP BY customer_id) d ON d.cid=p.customer_id
     WHERE p.last_login_at IS NOT NULL"""),
    # 同列相依：它吃 last_login_at 當輸入，所以那一欄一動就必須跟著重算。
    ("customer_profiles.last_active_at", "GREATEST(last_order_at, last_review_at, last_login_at)", """
     SELECT SUM(p.last_active_at = GREATEST(p.last_login_at,
                  COALESCE(p.last_order_at,p.last_login_at),
                  COALESCE(p.last_review_at,p.last_login_at))), COUNT(*), NULL, NULL
     FROM customer_profiles p WHERE p.last_active_at IS NOT NULL"""),
    ("customer_profiles.coupon_used_count", "COUNT(coupon_redemptions WHERE redeemed_at<=截止點)", """
     SELECT SUM(p.coupon_used_count=d.n), COUNT(*), SUM(p.coupon_used_count), SUM(d.n)
     FROM customer_profiles p JOIN (SELECT c.id cid,
       COALESCE(SUM(cr.redeemed_at<=:cut),0) n FROM customers c
       LEFT JOIN coupon_redemptions cr ON cr.customer_id=c.id GROUP BY c.id) d
       ON d.cid=p.customer_id WHERE p.coupon_used_count IS NOT NULL"""),
    ("customer_profiles.cart_abandon_count", "COUNT(carts WHERE is_abandoned AND created_at<=截止點)", """
     SELECT SUM(p.cart_abandon_count=d.n), COUNT(*), SUM(p.cart_abandon_count), SUM(d.n)
     FROM customer_profiles p JOIN (SELECT c.id cid,
       COALESCE(SUM(CASE WHEN ca.created_at<=:cut THEN ca.is_abandoned END),0) n
       FROM customers c LEFT JOIN carts ca ON ca.customer_id=c.id GROUP BY c.id) d
       ON d.cid=p.customer_id WHERE p.cart_abandon_count IS NOT NULL"""),
    ("customer_profiles.avg_order_value", "total_spent / order_count（同列）", """
     SELECT SUM(ABS(p.avg_order_value-p.total_spent/NULLIF(p.order_count,0))<0.5), COUNT(*),
       NULL, NULL FROM customer_profiles p
     WHERE p.avg_order_value IS NOT NULL AND p.order_count>0"""),

    # ---- review_profiles ----
    ("review_profiles.days_after_delivery", "created_at − MAX(shipments.delivered_at)", """
     SELECT SUM(p.days_after_delivery=d.dd), COUNT(*), NULL, NULL
     FROM review_profiles p JOIN (SELECT r.id rid,
       DATEDIFF(r.created_at, MAX(s.delivered_at)) dd FROM reviews r
       JOIN order_items oi ON oi.product_id=r.product_id
       JOIN orders o ON o.id=oi.order_id AND o.customer_id=r.customer_id
       JOIN shipments s ON s.order_id=o.id AND s.delivered_at IS NOT NULL
       GROUP BY r.id, r.created_at) d ON d.rid=p.review_id
     WHERE p.days_after_delivery IS NOT NULL"""),
    ("review_profiles.reply_count", "COUNT(review_replies)", """
     SELECT SUM(p.reply_count=d.n), COUNT(*), SUM(p.reply_count), SUM(d.n)
     FROM review_profiles p JOIN (SELECT r.id rid, COUNT(rr.id) n FROM reviews r
       LEFT JOIN review_replies rr ON rr.review_id=r.id GROUP BY r.id) d
       ON d.rid=p.review_id WHERE p.reply_count IS NOT NULL"""),
    ("review_profiles.has_merchant_reply", "EXISTS(review_replies)", """
     SELECT SUM(p.has_merchant_reply=(d.n>0)), COUNT(*), SUM(p.has_merchant_reply), SUM(d.n>0)
     FROM review_profiles p JOIN (SELECT r.id rid, COUNT(rr.id) n FROM reviews r
       LEFT JOIN review_replies rr ON rr.review_id=r.id GROUP BY r.id) d
       ON d.rid=p.review_id WHERE p.has_merchant_reply IS NOT NULL"""),
    ("review_profiles.reviewer_review_count", "COUNT(該顧客的 reviews)", """
     SELECT SUM(p.reviewer_review_count=d.n), COUNT(*), NULL, NULL
     FROM review_profiles p JOIN reviews r ON r.id=p.review_id
     JOIN (SELECT customer_id cid, COUNT(*) n FROM reviews GROUP BY customer_id) d
       ON d.cid=r.customer_id WHERE p.reviewer_review_count IS NOT NULL"""),
    ("review_profiles.first_posted_at", "reviews.created_at（母表同義欄）", """
     SELECT SUM(p.first_posted_at=r.created_at), COUNT(*), NULL, NULL
     FROM review_profiles p JOIN reviews r ON r.id=p.review_id
     WHERE p.first_posted_at IS NOT NULL"""),
    ("review_profiles.photo_count", "has_photo>0 應一致（同列）", """
     SELECT SUM(p.has_photo=(p.photo_count>0)), COUNT(*), NULL, NULL
     FROM review_profiles p WHERE p.photo_count IS NOT NULL AND p.has_photo IS NOT NULL"""),

    # ---- return_profiles ----
    ("return_profiles.requested_at", "order_returns.requested_at（同名欄）", """
     SELECT SUM(p.requested_at=o.requested_at), COUNT(*), NULL, NULL
     FROM return_profiles p JOIN order_returns o ON o.id=p.return_id
     WHERE p.requested_at IS NOT NULL"""),
    ("return_profiles.is_over_policy_window",
     "requested_at − orders.order_date > policy_window_days", """
     SELECT SUM(p.is_over_policy_window=(TIMESTAMPDIFF(DAY,o.order_date,p.requested_at)>p.policy_window_days)),
       COUNT(*), SUM(p.is_over_policy_window),
       SUM(TIMESTAMPDIFF(DAY,o.order_date,p.requested_at)>p.policy_window_days)
     FROM return_profiles p JOIN order_returns orr ON orr.id=p.return_id
     JOIN orders o ON o.id=orr.order_id WHERE p.is_over_policy_window IS NOT NULL"""),
    # 2026-08-25 修：原本寫成 transit_days + approval_days，量到 0/10 全紅 ——
    # 那是**檢查式錯了**，建表寫的是 (completed_at − requested_at)。
    # §8 ④「對照組與檢查工具本身也要驗」在這裡又中一次。
    ("return_profiles.total_days", "completed_at − requested_at，滿 24 小時算一天（同列）", """
     SELECT SUM(p.total_days=TIMESTAMPDIFF(DAY,p.requested_at,p.completed_at)), COUNT(*), NULL, NULL
     FROM return_profiles p WHERE p.total_days IS NOT NULL AND p.completed_at IS NOT NULL"""),

    # ---- 其餘 profile 表 ----
    ("shipment_profiles.attempt_count", "COUNT(delivery_attempts)", """
     SELECT SUM(p.attempt_count=d.n), COUNT(*), SUM(p.attempt_count), SUM(d.n)
     FROM shipment_profiles p JOIN (SELECT s.id sid, COUNT(da.id) n FROM shipments s
       LEFT JOIN delivery_attempts da ON da.shipment_id=s.id GROUP BY s.id) d
       ON d.sid=p.shipment_id WHERE p.attempt_count IS NOT NULL"""),
    ("product_profiles.image_count", "COUNT(product_images)", """
     SELECT SUM(p.image_count=d.n), COUNT(*), SUM(p.image_count), SUM(d.n)
     FROM product_profiles p JOIN (SELECT pr.id pid, COUNT(pi.id) n FROM products pr
       LEFT JOIN product_images pi ON pi.product_id=pr.id GROUP BY pr.id) d
       ON d.pid=p.product_id WHERE p.image_count IS NOT NULL"""),
    ("promotion_profiles.used_count", "COUNT(order_promotions)", """
     SELECT SUM(p.used_count=d.n), COUNT(*), SUM(p.used_count), SUM(d.n)
     FROM promotion_profiles p JOIN (SELECT pr.id pid, COUNT(op.id) n FROM promotions pr
       LEFT JOIN order_promotions op ON op.promotion_id=pr.id GROUP BY pr.id) d
       ON d.pid=p.promotion_id WHERE p.used_count IS NOT NULL"""),
    ("campaign_profiles.ctr_pct", "COUNT(campaign_clicks) 的相對排序", """
     SELECT SUM(p.rk=d.rk), COUNT(*), NULL, NULL FROM
       (SELECT campaign_id cid, RANK() OVER (ORDER BY ctr_pct DESC) rk FROM campaign_profiles) p
       JOIN (SELECT c.id cid, RANK() OVER (ORDER BY COUNT(cc.id) DESC) rk FROM campaigns c
         LEFT JOIN campaign_clicks cc ON cc.campaign_id=c.id GROUP BY c.id) d
       ON d.cid=p.cid"""),
    ("support_ticket_profiles.first_response_minutes",
     "first_response_at − support_tickets.created_at", """
     SELECT SUM(p.first_response_minutes=TIMESTAMPDIFF(MINUTE,t.created_at,p.first_response_at)),
       COUNT(*), NULL, NULL FROM support_ticket_profiles p
     JOIN support_tickets t ON t.id=p.ticket_id
     WHERE p.first_response_minutes IS NOT NULL AND p.first_response_at IS NOT NULL"""),
    ("support_ticket_profiles.prior_ticket_count", "COUNT(該顧客更早的 support_tickets)", """
     SELECT SUM(p.prior_ticket_count=d.n), COUNT(*), NULL, NULL
     FROM support_ticket_profiles p JOIN support_tickets t ON t.id=p.ticket_id
     JOIN (SELECT t1.id tid, COUNT(t2.id) n FROM support_tickets t1
       LEFT JOIN support_tickets t2 ON t2.customer_id=t1.customer_id
         AND t2.created_at<t1.created_at GROUP BY t1.id) d ON d.tid=t.id
     WHERE p.prior_ticket_count IS NOT NULL"""),
    ("subscription_profiles.total_billed_amount", "monthly_amount × billed_count（同列）", """
     SELECT SUM(ABS(p.total_billed_amount-p.monthly_amount*p.billed_count)<0.01), COUNT(*),
       NULL, NULL FROM subscription_profiles p
     WHERE p.total_billed_amount IS NOT NULL AND p.monthly_amount IS NOT NULL
       AND p.billed_count IS NOT NULL"""),
    ("payment_profiles.created_at", "payments.created_at（同名欄）", """
     SELECT SUM(p.created_at=pay.created_at), COUNT(*), NULL, NULL
     FROM payment_profiles p JOIN payments pay ON pay.id=p.payment_id
     WHERE p.created_at IS NOT NULL"""),
]

WORD = r"(?<![a-z0-9_])%s(?![a-z0-9_])"


def gt_columns():
    """哪些 profile 欄位真的被 GT 引用 —— 沒人問的欄位不一致也不會扣分，但會是地雷。"""
    gt = yaml.safe_load(io.open(os.path.join(_ROOT, "eval_ground_truth.yaml"),
                                encoding="utf-8"))
    cols = {name.split(".")[1] for name, _, _ in CHECKS}
    used = {}
    for x in gt:
        sql = (x.get("sql", "") + " " + " ".join(x.get("alt_sql", []) or [])).lower()
        for col in cols:
            if re.search(WORD % re.escape(col.lower()), sql):
                used.setdefault(col, []).append(x["id"])
    return used


def _snapshot_gap(cut):
    """快照族與即時值的差距 —— 這個差距**就是** #146/#147/#148 的鑑別力。

    上面的檢查保證「快照等於截止點的值」，但它保證不了「快照與即時值不同」。
    有人把快照對齊到即時值時，檢查照樣全綠，而那三題會靜默失去鑑別力。
    §7.9 的閘門 [10] 對題目層做過同一件事：**分歧名單變短本身就是警訊。**
    """
    sql = """
      SELECT SUM(p.order_count <> d.live_n), SUM(ABS(p.total_spent-d.live_s)>=0.01), COUNT(*)
      FROM customer_profiles p JOIN (SELECT c.id cid, COUNT(o.id) live_n,
        COALESCE(SUM(o.total_amount),0) live_s FROM customers c
        LEFT JOIN orders o ON o.customer_id=c.id GROUP BY c.id) d ON d.cid=p.customer_id"""
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        n_cnt, n_spent, total = c.execute(text(sql)).fetchone()
    print(f"\n快照落差（刻意的，不是缺陷）：截止點 {cut:%Y-%m-%d}｜"
          f"order_count 與即時值不同 {int(n_cnt)}/{total} 位｜"
          f"total_spent 不同 {int(n_spent)}/{total} 位")
    if not int(n_cnt) or not int(n_spent):
        print("    !! 落差歸零 —— #146/#147/#148 正在量一個沒有落差的東西，"
              "要嘛快照被誰對齊掉了，要嘛那三題該退役")


def main():
    log.remove()
    used = gt_columns()
    red, yellow, declared, ok = [], [], [], 0
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        print(f"{'':2s}{'欄位':50s} {'一致/可對照':>13s}  {'兩側合計':>17s}  引用")
        print("-" * 106)
        for name, how, sql in CHECKS:
            agree, total, ps, ds = c.execute(text(sql), {"cut": CUTOFF}).fetchone()
            agree, total = int(agree or 0), int(total or 0)
            rate = agree / total if total else 0.0
            qs = used.get(name.split(".")[1], [])
            sums = f"{int(ps):>8}/{int(ds):<8}" if ps is not None else " " * 17
            if rate == 1.0:
                mark, ok = ("快" if name in SNAPSHOT else "  "), ok + 1
            elif name in DECLARED:
                mark = "宣"
                declared.append(name)
            elif qs or name in DERIVED:
                mark = "紅"
                red.append((name, how, agree, total, qs))
            else:
                mark = "黃"
                yellow.append((name, how, agree, total))
            print(f"{mark}{name:50s} {agree:>5d}/{total:<5d} {rate:5.0%} {sums} "
                  + (f"#{',#'.join(map(str, qs[:4]))}" if qs else "—"))
    print("-" * 106)
    print(f"\n一致 {ok}｜已宣告 {len(declared)}｜黃燈（地雷）{len(yellow)}｜紅燈 {len(red)}")

    if yellow:
        print("\n黃燈 —— 值與母表不符，但目前沒有題目引用。"
              "下次配題只要問到這些欄位就會變紅燈：")
        for name, how, a, t in sorted(yellow, key=lambda x: x[2] / max(x[3], 1)):
            print(f"    {name:48s} {a}/{t}   ← {how}")
    if red:
        print("\n紅燈 —— 有題目在量一個沒有唯一答案的東西，或衍生步驟沒有跑。"
              "先跑 `python tools/fix_derived_consistency.py --apply`；"
              "還是不一致才是真缺陷，三選一：對齊資料 / 收窄問句 / 寫進 DECLARED：")
        for name, how, a, t, qs in sorted(red, key=lambda x: x[2] / max(x[3], 1)):
            print(f"    {name:48s} {a}/{t}   ← {how}\n"
                  f"    {'':48s} 題號 {qs}")
        sys.exit(1)
    _snapshot_gap(CUTOFF)
    print("\n閘門 [9] 綠燈：沒有「有題目引用且未宣告」的矛盾欄位。")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
