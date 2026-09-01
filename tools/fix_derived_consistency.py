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

**第二輪：清掉閘門 [9] 的 9 個黃燈（2026-08-26）**

黃燈 = 值與母表不符、但目前沒有題目引用。今天不扣分，**下一批配題只要問到就變紅燈**。
上一輪的教訓正是「修完一題沒有人問還有幾個」（§9），所以這次一次清完。

這 9 欄分成兩族，處置不同：

  **快照族**（customer_profiles 的 4 欄）—— 欄位註解本來就寫著「（快照）」，
  而 order_count / total_spent 實測**剛好等於** anchor − 30 天的截止值（50/50）。
  也就是說這張表的身分是「每日結算快照」，那 4 欄卻是 R.randint(...)。
  對齊到**同一個截止點**，不是對齊到即時值 —— 對齊到即時值會讓同一張表裡
  一半欄位是快照、一半是即時，那是更難發現的矛盾。
    total_items_bought / return_count / coupon_used_count / cart_abandon_count

  **即時族**（其餘 5 欄）—— 欄位註解沒有宣告快照，母表唯一決定，直接對齊。
    customer_profiles.last_login_at（＋連帶重算 last_active_at）
    review_profiles.reviewer_review_count / reply_count / has_merchant_reply（＋merchant_reply_at）
    support_ticket_profiles.prior_ticket_count

**同一輪把閘門也升級了**：快照族原本只能寫進 DECLARED（宣告成不檢查），
現在改成「用截止點檢查」——**從『宣告的例外』變成『可驗證的不變量』**。
宣告只說「這兩個量本來就不同」，檢查能說「它等於它該等於的那個量」。

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
from datetime import timedelta

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.data_anchor import DATA_ANCHOR_DATETIME  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

# customer_profiles 的快照截止點。SNAPSHOT_LAG_DAYS 與 init_db_ext.seed_customer_profiles
# 同源；實測 order_count / total_spent 對這個截止點是 50/50 全中，所以它不是猜的。
SNAPSHOT_LAG_DAYS = 30
CUTOFF = DATA_ANCHOR_DATETIME - timedelta(days=SNAPSHOT_LAG_DAYS)

# (表, 欄, 說明, UPDATE)。每一條都要能重複執行。
#
# ⚠️ 這張表現在裝了**兩類**東西，判準不同，不要混在一起讀：
#
#   [9]  縱向：寬表的衍生欄位 vs 從**母表**現算。錯的一定是寬表那一欄。
#   [11] 橫向：兩張**平行**的表對同一件事各擲各的骰子，誰也不衍生自誰。
#        沒有母表可以現算，所以要**先裁決誰是真相來源**才知道要改哪一邊。
#        裁決寫在 `tools/check_predicate_collisions.py` 的 ADJUDICATED，含理由。
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

    # ================= 第二輪：閘門 [9] 的 9 個黃燈（2026-08-26）=================
    # ---- 快照族：對齊到 anchor − 30 天，不是對齊到即時值 ----
    ("customer_profiles", "total_items_bought",
     "SUM(order_items.quantity)，只算截止點之前的訂單。原本是 len(snap)*randint(1,4) "
     "—— 用了 snap 所以看起來像快照，乘上亂數之後就不是了（50 位只有 2 位對得上）。",
     """UPDATE customer_profiles p
        JOIN (SELECT c.id cid,
                     COALESCE(SUM(CASE WHEN o.order_date<=:cut THEN oi.quantity END),0) n
              FROM customers c
              LEFT JOIN orders o ON o.customer_id=c.id
              LEFT JOIN order_items oi ON oi.order_id=o.id
              GROUP BY c.id) d ON d.cid=p.customer_id
        SET p.total_items_bought = d.n"""),

    ("customer_profiles", "return_count",
     "COUNT(order_returns via orders)，事件時間用 requested_at，只算截止點之前。"
     "原本是 choices([0,1,2])，與這位客戶有沒有退貨無關。",
     """UPDATE customer_profiles p
        JOIN (SELECT c.id cid,
                     COALESCE(SUM(CASE WHEN orr.requested_at<=:cut THEN 1 END),0) n
              FROM customers c
              LEFT JOIN orders o ON o.customer_id=c.id
              LEFT JOIN order_returns orr ON orr.order_id=o.id
              GROUP BY c.id) d ON d.cid=p.customer_id
        SET p.return_count = d.n"""),

    ("customer_profiles", "coupon_used_count",
     "COUNT(coupon_redemptions)，事件時間用 redeemed_at，只算截止點之前。"
     "原本是 randint(0,4)：合計 85 而實際兌換只有 56 筆。",
     """UPDATE customer_profiles p
        JOIN (SELECT c.id cid,
                     COALESCE(SUM(CASE WHEN cr.redeemed_at<=:cut THEN 1 END),0) n
              FROM customers c
              LEFT JOIN coupon_redemptions cr ON cr.customer_id=c.id
              GROUP BY c.id) d ON d.cid=p.customer_id
        SET p.coupon_used_count = d.n"""),

    ("customer_profiles", "cart_abandon_count",
     "COUNT(carts WHERE is_abandoned)，事件時間用 carts.created_at，只算截止點之前。"
     "原本是 randint(0,5)：合計 120 而實際放棄的購物車只有 38 台。",
     """UPDATE customer_profiles p
        JOIN (SELECT c.id cid,
                     COALESCE(SUM(CASE WHEN ca.created_at<=:cut THEN ca.is_abandoned END),0) n
              FROM customers c
              LEFT JOIN carts ca ON ca.customer_id=c.id
              GROUP BY c.id) d ON d.cid=p.customer_id
        SET p.cart_abandon_count = d.n"""),

    # ---- 即時族：母表唯一決定，沒有宣告快照 ----
    ("customer_profiles", "last_login_at",
     "MAX(customer_login_logs.logged_in_at WHERE success=1)。原本是 anchor − randint(0,120) 天，"
     "0/50 對得上 —— 而 customer_login_logs 的表註解自己寫著「最近一次登入時間在 "
     "customer_profiles.last_login_at」，等於指著一個亂數說那是答案。"
     "只算成功的登入：失敗的嘗試不是「登入過」，全部 50 位都有成功紀錄，不會產生 NULL。",
     """UPDATE customer_profiles p
        JOIN (SELECT customer_id cid, MAX(logged_in_at) m
              FROM customer_login_logs WHERE success=1 GROUP BY customer_id) d
          ON d.cid=p.customer_id
        SET p.last_login_at = d.m"""),

    ("customer_profiles", "last_active_at",
     "GREATEST(last_order_at, last_review_at, last_login_at) —— 與 seed 的定義相同。"
     "它吃 last_login_at 當輸入，所以上一條改完必須跟著重算，否則會出現"
     "「最近活動早於最近登入」這種自相矛盾的列。",
     """UPDATE customer_profiles p
        SET p.last_active_at = GREATEST(
              p.last_login_at,
              COALESCE(p.last_order_at,  p.last_login_at),
              COALESCE(p.last_review_at, p.last_login_at))"""),

    ("review_profiles", "reviewer_review_count",
     "COUNT(該客戶的 reviews)。review_id → reviews.customer_id 路徑唯一。原本是 randint(1,9)。",
     """UPDATE review_profiles p
        JOIN reviews r ON r.id=p.review_id
        JOIN (SELECT customer_id cid, COUNT(*) n FROM reviews GROUP BY customer_id) d
          ON d.cid=r.customer_id
        SET p.reviewer_review_count = d.n"""),

    ("review_profiles", "reply_count",
     "COUNT(review_replies)。review_id 是 FK，路徑唯一。"
     "原本是 randint(1,3) if merchant else 0：合計 123 而 review_replies 只有 33 列。",
     """UPDATE review_profiles p
        JOIN (SELECT r.id rid, COUNT(rr.id) n FROM reviews r
              LEFT JOIN review_replies rr ON rr.review_id=r.id GROUP BY r.id) d
          ON d.rid=p.review_id
        SET p.reply_count = d.n"""),

    ("review_profiles", "has_merchant_reply",
     "EXISTS(review_replies)，連同 merchant_reply_at 一起對齊成 MIN(replied_at)。"
     "原本旗標是 random()<0.4（57 列為 1）而實際有回覆的只有 33 則 —— "
     "旗標、次數、時間三個欄位各自獨立亂數，彼此也對不起來。",
     """UPDATE review_profiles p
        LEFT JOIN (SELECT review_id rid, COUNT(*) n, MIN(replied_at) t
                   FROM review_replies GROUP BY review_id) d
          ON d.rid=p.review_id
        SET p.has_merchant_reply = IF(d.n IS NULL, 0, 1),
            p.merchant_reply_at  = d.t"""),

    ("support_ticket_profiles", "prior_ticket_count",
     "COUNT(同一客戶、created_at 更早的 support_tickets)。ticket_id 是 FK，路徑唯一。"
     "原本是 randint(0,5)。",
     """UPDATE support_ticket_profiles p
        JOIN support_tickets t ON t.id=p.ticket_id
        JOIN (SELECT t1.id tid, COUNT(t2.id) n FROM support_tickets t1
              LEFT JOIN support_tickets t2
                ON t2.customer_id=t1.customer_id AND t2.created_at<t1.created_at
              GROUP BY t1.id) d ON d.tid=t.id
        SET p.prior_ticket_count = d.n"""),

    # ------------------------------------------------------------------
    # 以下是閘門 [11] 的橫向衝突（2026-09-01，§7.10／§7.11）
    # ------------------------------------------------------------------
    ("product_specs", "is_discontinued",
     "【橫向】真相來源＝product_profiles。「這個商品停產了沒」被寫在兩張**兄弟**表：\n"
     "     product_specs.is_discontinued  『是否已停產』        1 筆\n"
     "     product_profiles.lifecycle_stage『EOL 停產』        12 筆   交集 0\n"
     "     選 product_profiles 當真相：lifecycle_stage 與 delisted_at 兩欄互相佐證\n"
     "     （各 12 筆、互斥集合皆為 0），is_discontinued 只有 1 筆孤證。\n"
     "     它原本是 db/init_db_ext.py:648 的 `1 if random.random() < 0.1 else 0`，\n"
     "     而 product_profiles 由 tools/add_wide_tables*.py 在之後才建 ——\n"
     "     **建表時結構上無法對齊**，與 customer_profiles 同型（§7.9）。\n"
     "     影響：#127「已經停產的商品有哪些？」由 1 列變 12 列；#258 的 GT 不變，\n"
     "     但模型走 is_discontinued 那條路不再得到互斥的答案。",
     """UPDATE product_specs ps
        JOIN product_profiles pp ON pp.product_id = ps.product_id
        SET ps.is_discontinued = (pp.lifecycle_stage = 'EOL')"""),

    ("order_returns", "status",
     "【橫向】真相來源＝return_shipments.received_at（有時間戳，較具體）。\n"
     "     「倉庫收到了沒」被寫在兩處，實測 4 筆矛盾：status='RECEIVED' 但\n"
     "     該筆退貨要嘛沒有物流單（2 筆），要嘛 received_at 是 NULL（2 筆）。\n"
     "     gen_order_returns 用 RNG 抽 status（第一波），gen_return_shipments\n"
     "     用 RNG_LATE 抽 received_at（第二波）—— 前後兩波，誰也不知道對方。\n"
     "     只修「宣稱收到但沒收到」這個方向；REJECTED 而 received_at 有值是\n"
     "     合法的（先收到再拒絕退貨）。影響：#168 由 10 列變 6 列。",
     """UPDATE order_returns r
        SET r.status = 'REQUESTED'
        WHERE r.status = 'RECEIVED'
          AND NOT EXISTS (SELECT 1 FROM return_shipments rs
                          WHERE rs.return_id = r.id AND rs.received_at IS NOT NULL)"""),

    # ==================================================================
    # 第三輪：全庫掃描（2026-09-02，§7.12）
    #
    # 前兩條是「手上有題目、順著查出來的」。這一輪反過來：不靠題目，
    # 掃全庫每一對共用母表的表，找**同一個事實被寫成兩欄而值對不上**。
    # 掃描腳本的兩個 bug 各自藏起一批（collation 例外被吞掉、兩跳 JOIN
    # 別名寫反），修掉之後才看得到 supplier payment_terms 與
    # order_profiles.installment_periods 這幾組。
    #
    # **真相來源的判準（三組促銷欄位各判到不同邊，不是隨便挑的）**：
    #   有佐證的贏 —— 同一張表裡有別的欄位獨立指向同一個事實
    #   都沒佐證  -> 原始窄表贏，後加的寬表是獨立擲的骰子（§7.9 的形狀）
    # ==================================================================
    ("customer_profiles", "newsletter_opt_in",
     "【橫向】真相來源＝newsletter_subscriptions。『是否訂閱電子報』與\n"
     "     『退訂時間，仍訂閱中為空』是同一件事，實測 22 vs 18、交集只有 3。\n"
     "     opt_in 是 db/init_db_ext.py:782 的 35% 獨立硬幣，另一邊有\n"
     "     subscribed_at／unsubscribed_at 兩個時間戳互相佐證 —— 有佐證的贏。\n"
     "     GT **零引用** newsletter_opt_in，所以不動任何一題的答案；\n"
     "     #237 走的 unsubscribed_at IS NULL 仍然是 18。",
     """UPDATE customer_profiles p
        SET p.newsletter_opt_in = EXISTS(
              SELECT 1 FROM newsletter_subscriptions n
              WHERE n.customer_id = p.customer_id AND n.unsubscribed_at IS NULL)"""),

    ("promotion_profiles", "is_stackable",
     "【橫向】真相來源＝promotion_rules。**#194 與 #302 是 #127／#258 的翻版**：\n"
     "     #194『有幾條促銷規則是可以疊加使用的？』-> promotion_rules.stackable\n"
     "     #302『列出不可與其他優惠疊加的檔期…』  -> promotion_profiles.is_stackable\n"
     "     同一個詞「疊加」，兩題各走一欄，8 檔裡有 2 檔答案相反。\n"
     "     兩邊都沒有佐證（is_exclusive 全 0、stack_priority 不分疊不疊都有值），\n"
     "     所以照「都沒佐證就原始窄表贏」判給 promotion_rules。#302 的列會換人。",
     """UPDATE promotion_profiles pp
        JOIN promotion_rules pr ON pr.promotion_id = pp.promotion_id
        SET pp.is_stackable = pr.stackable"""),

    ("promotion_profiles", "min_order_amount",
     "【橫向】真相來源＝promotion_rules。同上一條的形狀：\n"
     "     #195『促銷規則的最低消費門檻，最高是多少？』-> promotion_rules.min_amount\n"
     "     #305『…各自的最低訂單門檻…』              -> promotion_profiles.min_order_amount\n"
     "     8 檔有 7 檔對不上，而且 min_order_amount 有 5 檔都是 500（預設值的樣子），\n"
     "     rules 那邊是 1000~4000。沒有佐證欄位 -> 原始窄表贏。#305 的數字會變。",
     """UPDATE promotion_profiles pp
        JOIN promotion_rules pr ON pr.promotion_id = pp.promotion_id
        SET pp.min_order_amount = pr.min_amount"""),

    ("promotion_rules", "applies_to",
     "【橫向】真相來源＝promotion_profiles ——**這一條判給寬表，方向與上面兩條相反**。\n"
     "     『適用範圍』被寫成兩欄，8 檔有 6 檔對不上。這次寬表有佐證：\n"
     "     applies_to_scope='CATEGORY' 的 3 檔 included_category 全部有值，\n"
     "     ='ALL' 的 3 檔全部是 NULL，完全吻合欄位註解宣告的不變量；\n"
     "     rules.applies_to 則沒有任何欄位佐證，也沒有題目引用它。\n"
     "     SKU 與 PRODUCT 是同一個意思，BRAND 是 rules 表達不了的層級 ——\n"
     "     所以順帶把 rules 的列舉註解補上 BRAND（tools/add_distractor_tables.py）。",
     """UPDATE promotion_rules pr
        JOIN promotion_profiles pp ON pp.promotion_id = pr.promotion_id
        SET pr.applies_to = REPLACE(pp.applies_to_scope, 'SKU', 'PRODUCT')"""),

    ("supplier_profiles", "payment_terms",
     "【橫向】真相來源＝supplier_contracts（**同欄名、8/8 全部對不上**）。\n"
     "     兩邊用不同的詞彙寫同一件事：合約寫『月結60天』，檔案寫『NET60』。\n"
     "     每家供應商剛好各 1 份合約、1 種付款條件，所以對應是唯一的；\n"
     "     #193 就是走 supplier_contracts 的中文值。對照表：\n"
     "       月結30天->NET30  月結60天->NET60  預付訂金->PREPAID  貨到付款->COD\n"
     "     COD 原本不在 supplier_profiles 的列舉裡 —— 已補進欄位註解，\n"
     "     否則對齊會產生一個註解說不存在的值（tools/wide_table_plan.yaml）。",
     """UPDATE supplier_profiles sp
        JOIN supplier_contracts sc ON sc.supplier_id = sp.supplier_id
        SET sp.payment_terms = CASE sc.payment_terms
              WHEN '月結30天' THEN 'NET30' WHEN '月結60天' THEN 'NET60'
              WHEN '預付訂金' THEN 'PREPAID' WHEN '貨到付款' THEN 'COD'
              ELSE sp.payment_terms END"""),

    ("order_profiles", "installment_periods",
     "【橫向】真相來源＝payment_profiles（**同欄名、註解幾乎逐字相同**）：\n"
     "       order_profiles.installment_periods  『分期期數，0 表示一次付清』\n"
     "       payment_profiles.installment_periods『分期期數，未分期為 0』\n"
     "     164 筆有 68 筆對不上：38 張訂單這邊寫 3/6/12 而那邊是 0，\n"
     "     21 張反過來，而且 order_profiles 根本沒有 24 期這個值。\n"
     "     payment_profiles 有 is_installment／is_zero_interest／first_payment_date／\n"
     "     acquirer_name 一整組互相佐證，#291 也走它 -> 有佐證的贏。\n"
     "     沒有付款紀錄的 36 張訂單設 0（＝一次付清，不是 NULL）。",
     """UPDATE order_profiles op
        LEFT JOIN payments py ON py.order_id = op.order_id
        LEFT JOIN payment_profiles pp ON pp.payment_id = py.id
        SET op.installment_periods = COALESCE(pp.installment_periods, 0)"""),

    ("order_profiles", "currency_code",
     "【橫向】真相來源＝payment_profiles。同欄名，164 筆有 10 筆對不上，\n"
     "     而且錯得很整齊：那 10 筆訂單寫 TWD／匯率 1.0，付款那邊寫 USD／匯率 30~32。\n"
     "     同一筆交易不可能同時是台幣又是美金。付款端是真正的結算紀錄，\n"
     "     幣別與匯率兩欄互相佐證 -> 有佐證的贏。exchange_rate 一起對齊（見下一條），\n"
     "     否則會留下「幣別是 USD 但匯率是 1」這種更難發現的半對齊。",
     """UPDATE order_profiles op
        JOIN payments py ON py.order_id = op.order_id
        JOIN payment_profiles pp ON pp.payment_id = py.id
        SET op.currency_code = pp.currency_code"""),

    ("order_profiles", "exchange_rate",
     "【橫向】接上一條：幣別對齊了，匯率必須跟著對齊，否則自相矛盾。",
     """UPDATE order_profiles op
        JOIN payments py ON py.order_id = op.order_id
        JOIN payment_profiles pp ON pp.payment_id = py.id
        SET op.exchange_rate = pp.exchange_rate"""),

    ("invoices", "carrier",
     "【領域不變量】發票類型 DONATE 捐贈的 15 張發票**全部**有載具號碼。\n"
     "     捐贈與載具在開立電子發票時是互斥的選項（捐了就沒有載具可歸戶），\n"
     "     所以『發票類型＝捐贈』與『載具號碼有值』不可能同時成立。\n"
     "     沒有任何欄位註解寫出這條規則，也沒有題目問到 —— 但它跟\n"
     "     「外包裝比商品本體還小」是同一類：**列舉值自己的語意就排除了它**。\n"
     "     必須排在 order_profiles.invoice_carrier 對齊**之前**，否則那一條\n"
     "     會把剛清掉的載具再抄回去。",
     """UPDATE invoices i
        JOIN order_profiles op ON op.order_id = i.order_id
        SET i.carrier = NULL
        WHERE op.invoice_type = 'DONATE'"""),

    ("order_profiles", "invoice_carrier",
     "【橫向】真相來源＝invoices.carrier『載具號碼』。164 筆**全部**對不上 ——\n"
     "     兩邊各產生各的字串，等於同一張發票有兩個載具號。\n"
     "     invoices 是原始表且 #160~#162 都走它。沒開發票的 36 張訂單設 NULL\n"
     "     （原本 36 張全部有值，那是憑空長出來的載具）。",
     """UPDATE order_profiles op
        LEFT JOIN invoices i ON i.order_id = op.order_id
        SET op.invoice_carrier = i.carrier"""),

    ("order_profiles", "is_split_payment",
     "【橫向】真相來源＝payments 的列數。『是否分多筆付款完成』宣稱 18 張訂單\n"
     "     分多筆付款，但全庫**沒有任何一張訂單有 2 筆以上的 payments**\n"
     "     （最多 1 筆），其中 3 張甚至一筆付款都沒有。對齊後整欄變 0。\n"
     "     一個恆為 0 的欄位沒有用處，但它至少不再說謊；\n"
     "     GT 零引用，所以不動任何答案。注意：這與『分期』是兩件事 ——\n"
     "     分期是一筆付款拆成多期（payment_profiles.is_installment），\n"
     "     分多筆是多筆 payments，兩者不該互相對齊。",
     """UPDATE order_profiles op
        SET op.is_split_payment =
            ((SELECT COUNT(*) FROM payments y WHERE y.order_id = op.order_id) >= 2)"""),

    ("return_profiles", "received_at",
     "【橫向】真相來源＝return_shipments.received_at，與 order_returns.status\n"
     "     那一條同一個裁決（§7.10）。同欄名，14 筆有 13 筆對不上：\n"
     "     18 筆退貨裡有 7 筆根本沒寄回或倉庫還沒收到，這一欄卻都填了收件時間。\n"
     "     沒有物流單或還沒收到的一律設 NULL —— 註解本來就寫『尚未收到為空』。",
     """UPDATE return_profiles p
        LEFT JOIN return_shipments rs ON rs.return_id = p.return_id
        SET p.received_at = rs.received_at"""),

    ("return_profiles", "transit_days",
     "【橫向＋縱向】『從寄回到收到經過幾天』＝ received_at − shipped_at，\n"
     "     路徑唯一。閘門 [9] 看不到它，因為 return_profiles 的母表是\n"
     "     order_returns，而算這一欄要走到 return_shipments（隔一張表）。\n"
     "     原本 18 筆全是亂數，連沒收到的都填了天數。影響：#307 的天數會變。",
     """UPDATE return_profiles p
        LEFT JOIN return_shipments rs ON rs.return_id = p.return_id
        SET p.transit_days = IF(rs.received_at IS NULL, NULL,
                                TIMESTAMPDIFF(DAY, rs.shipped_at, rs.received_at))"""),

    ("product_specs", "energy_label",
     "【橫向】真相來源＝product_profiles.energy_label（同欄名，7/7 對不上）。\n"
     "       product_specs   『能源效率標示』      NULL 33 筆、'N級' 7 筆\n"
     "       product_profiles『能源效率標示等級：1~5 級』 40 筆都有\n"
     "     只改那 7 筆有值的，NULL 保持 NULL（沒有能源標示的商品是合理的，\n"
     "     把它填滿等於憑空造資料）。與 is_discontinued 同一組表、同一個方向。",
     """UPDATE product_specs ps
        JOIN product_profiles pp ON pp.product_id = ps.product_id
        SET ps.energy_label = CONCAT(pp.energy_label, '級')
        WHERE ps.energy_label IS NOT NULL"""),

    ("subscriptions", "ended_at",
     "【橫向】『訂閱結束日，**仍在訂閱中為空**』—— 但 5 筆 PAUSED 的訂閱全部\n"
     "     有結束日，而它們的 subscription_profiles.is_paused=1、\n"
     "     resume_scheduled_at 也全部排好了復訂日。暫停中的訂閱沒有結束，\n"
     "     三個欄位有兩個說「還在」、一個說「結束了」。\n"
     "     影響：#212『有幾筆訂閱還沒有結束？』由 6 變 11（ACTIVE 6 + PAUSED 5），\n"
     "     這是這一輪唯一一題**答案數字改變且該改**的題目。",
     """UPDATE subscriptions s SET s.ended_at = NULL WHERE s.status = 'PAUSED'"""),

    ("delivery_attempts", "result",
     "【橫向】真相來源＝shipments.status —— delivery_attempts 的**表註解自己**\n"
     "     就寫著『最終是否送達看 shipments.status』。但 38 張 IN_TRANSIT 的\n"
     "     出貨單**全部**有一筆 result='DELIVERED' 的派送紀錄，而且是最後一次\n"
     "     嘗試。既然還在運送中，那次派送就不可能已經送達。\n"
     "     改成 ABSENT（撲空）：**只改值、不增刪列**，所以 #177（嘗試 3 次的\n"
     "     出貨單）與 shipment_profiles.attempt_count 都不受影響；\n"
     "     #176（配送失敗過的出貨單）會變多。",
     """UPDATE delivery_attempts d
        JOIN shipments s ON s.id = d.shipment_id
        SET d.result = 'ABSENT'
        WHERE d.result = 'DELIVERED' AND s.status <> 'DELIVERED'"""),

    ("store_profiles", "mon_open",
     "【橫向】stores.is_active=0『是否仍在營業』的門市（嘉義市門市），\n"
     "     store_profiles 卻還寫著週一到週日的營業時間與每週 62.8 小時。\n"
     "     一間已經停業的店不會有本週營業時間。七天一起設成 CLOSED\n"
     "     （欄位註解本來就寫『CLOSED 表示公休』），週時數歸零、24 小時旗標關掉。\n"
     "     GT 零引用這些欄位；#200 走 stores.is_active，不受影響。",
     """UPDATE store_profiles p
        JOIN stores s ON s.id = p.store_id
        SET p.mon_open='CLOSED', p.tue_open='CLOSED', p.wed_open='CLOSED',
            p.thu_open='CLOSED', p.fri_open='CLOSED', p.sat_open='CLOSED',
            p.sun_open='CLOSED', p.weekly_open_hours=0, p.is_24h=0
        WHERE s.is_active = 0"""),

    ("product_profiles", "package_weight_g",
     "【物理不變量】『**含包裝**的總重量』必須 >= product_specs.weight_g『重量』，\n"
     "     40 項有 16 項比商品本體還輕。這不是兩張表對同一件事各說各話，\n"
     "     是註解裡的『含』宣告了一個不變量而資料違反它。\n"
     "     補的差額用**這張表自己 24 筆合法列的中位數包材重**，不是我挑的數字。\n"
     "     影響：#259 會拿到修正後的重量。",
     """UPDATE product_profiles pp
        JOIN product_specs ps ON ps.product_id = pp.product_id
        JOIN (SELECT CAST(AVG(m) AS SIGNED) med FROM (
                SELECT p2.package_weight_g - s2.weight_g m
                FROM product_profiles p2 JOIN product_specs s2 ON s2.product_id=p2.product_id
                WHERE p2.package_weight_g >= s2.weight_g
                ORDER BY m LIMIT 2 OFFSET 11) t) d
        SET pp.package_weight_g = ps.weight_g + d.med
        WHERE pp.package_weight_g < ps.weight_g"""),

    ("product_profiles", "package_length_cm",
     "【物理不變量】接上一條，三個維度同理：『**外包裝**長／寬／高（公分）』\n"
     "     不可能小於商品本體的長／寬／高（公釐）。實測 20／19／10 項違反。\n"
     "     一律改成「商品尺寸換算成公分後無條件進位再加 1 公分」——\n"
     "     加 1 是包材厚度的下限，不是估的裝箱餘裕。三欄在同一條 UPDATE 裡改，\n"
     "     分開改會讓中間狀態出現「長對了寬還沒對」的列。",
     """UPDATE product_profiles pp
        JOIN product_specs ps ON ps.product_id = pp.product_id
        SET pp.package_length_cm = IF(pp.package_length_cm*10 < ps.length_mm,
                                      CEIL(ps.length_mm/10)+1, pp.package_length_cm),
            pp.package_width_cm  = IF(pp.package_width_cm*10  < ps.width_mm,
                                      CEIL(ps.width_mm/10)+1,  pp.package_width_cm),
            pp.package_height_cm = IF(pp.package_height_cm*10 < ps.height_mm,
                                      CEIL(ps.height_mm/10)+1, pp.package_height_cm)"""),
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
        # ---- 第二輪：快照族（比的是 anchor − 30 天的截止值）----
        "customer_profiles.total_items_bought": """
            SELECT SUM(p.total_items_bought<>d.n), SUM(p.total_items_bought), SUM(d.n)
            FROM customer_profiles p JOIN (SELECT c.id cid,
              COALESCE(SUM(CASE WHEN o.order_date<=:cut THEN oi.quantity END),0) n
              FROM customers c LEFT JOIN orders o ON o.customer_id=c.id
              LEFT JOIN order_items oi ON oi.order_id=o.id GROUP BY c.id) d
              ON d.cid=p.customer_id""",
        "customer_profiles.return_count": """
            SELECT SUM(p.return_count<>d.n), SUM(p.return_count), SUM(d.n)
            FROM customer_profiles p JOIN (SELECT c.id cid,
              COALESCE(SUM(CASE WHEN orr.requested_at<=:cut THEN 1 END),0) n
              FROM customers c LEFT JOIN orders o ON o.customer_id=c.id
              LEFT JOIN order_returns orr ON orr.order_id=o.id GROUP BY c.id) d
              ON d.cid=p.customer_id""",
        "customer_profiles.coupon_used_count": """
            SELECT SUM(p.coupon_used_count<>d.n), SUM(p.coupon_used_count), SUM(d.n)
            FROM customer_profiles p JOIN (SELECT c.id cid,
              COALESCE(SUM(CASE WHEN cr.redeemed_at<=:cut THEN 1 END),0) n
              FROM customers c LEFT JOIN coupon_redemptions cr ON cr.customer_id=c.id
              GROUP BY c.id) d ON d.cid=p.customer_id""",
        "customer_profiles.cart_abandon_count": """
            SELECT SUM(p.cart_abandon_count<>d.n), SUM(p.cart_abandon_count), SUM(d.n)
            FROM customer_profiles p JOIN (SELECT c.id cid,
              COALESCE(SUM(CASE WHEN ca.created_at<=:cut THEN ca.is_abandoned END),0) n
              FROM customers c LEFT JOIN carts ca ON ca.customer_id=c.id
              GROUP BY c.id) d ON d.cid=p.customer_id""",
        # ---- 第二輪：即時族 ----
        "customer_profiles.last_login_at": """
            SELECT SUM(DATE(p.last_login_at)<>DATE(d.m)), NULL, NULL
            FROM customer_profiles p JOIN (SELECT customer_id cid, MAX(logged_in_at) m
              FROM customer_login_logs WHERE success=1 GROUP BY customer_id) d
              ON d.cid=p.customer_id""",
        "customer_profiles.last_active_at": """
            SELECT SUM(p.last_active_at <> GREATEST(p.last_login_at,
                       COALESCE(p.last_order_at,p.last_login_at),
                       COALESCE(p.last_review_at,p.last_login_at))), NULL, NULL
            FROM customer_profiles p""",
        "review_profiles.reviewer_review_count": """
            SELECT SUM(p.reviewer_review_count<>d.n), NULL, NULL
            FROM review_profiles p JOIN reviews r ON r.id=p.review_id
            JOIN (SELECT customer_id cid, COUNT(*) n FROM reviews GROUP BY customer_id) d
              ON d.cid=r.customer_id""",
        "review_profiles.reply_count": """
            SELECT SUM(p.reply_count<>d.n), SUM(p.reply_count), SUM(d.n)
            FROM review_profiles p JOIN (SELECT r.id rid, COUNT(rr.id) n FROM reviews r
              LEFT JOIN review_replies rr ON rr.review_id=r.id GROUP BY r.id) d
              ON d.rid=p.review_id""",
        "review_profiles.has_merchant_reply": """
            SELECT SUM(p.has_merchant_reply<>(d.n>0)), SUM(p.has_merchant_reply), SUM(d.n>0)
            FROM review_profiles p JOIN (SELECT r.id rid, COUNT(rr.id) n FROM reviews r
              LEFT JOIN review_replies rr ON rr.review_id=r.id GROUP BY r.id) d
              ON d.rid=p.review_id""",
        "support_ticket_profiles.prior_ticket_count": """
            SELECT SUM(p.prior_ticket_count<>d.n), NULL, NULL
            FROM support_ticket_profiles p JOIN support_tickets t ON t.id=p.ticket_id
            JOIN (SELECT t1.id tid, COUNT(t2.id) n FROM support_tickets t1
              LEFT JOIN support_tickets t2 ON t2.customer_id=t1.customer_id
                AND t2.created_at<t1.created_at GROUP BY t1.id) d ON d.tid=t.id""",
        # ---- 閘門 [11]：橫向衝突（2026-09-01）----
        "product_specs.is_discontinued": """
            SELECT SUM(ps.is_discontinued <> (pp.lifecycle_stage='EOL')),
                   SUM(ps.is_discontinued), SUM(pp.lifecycle_stage='EOL')
            FROM product_specs ps JOIN product_profiles pp ON pp.product_id=ps.product_id""",
        "order_returns.status": """
            SELECT SUM(r.status='RECEIVED' AND NOT EXISTS(SELECT 1 FROM return_shipments rs
                       WHERE rs.return_id=r.id AND rs.received_at IS NOT NULL)),
                   SUM(r.status='RECEIVED'),
                   SUM(r.status='RECEIVED' AND EXISTS(SELECT 1 FROM return_shipments rs
                       WHERE rs.return_id=r.id AND rs.received_at IS NOT NULL))
            FROM order_returns r""",
        # ---- 第三輪：全庫掃描找出來的橫向衝突（2026-09-02）----
        # 字串欄位一律加 COLLATE：兩批表建立時的 collation 不同
        # （utf8mb4_0900_ai_ci vs utf8mb4_unicode_ci），直接比會丟例外。
        # 掃描腳本第一版就是把這個例外吞掉，於是 supplier payment_terms
        # 與 invoice_carrier 整整兩組**看起來像通過**。
        "customer_profiles.newsletter_opt_in": """
            SELECT SUM(p.newsletter_opt_in <> EXISTS(SELECT 1 FROM newsletter_subscriptions n
                       WHERE n.customer_id=p.customer_id AND n.unsubscribed_at IS NULL)),
                   SUM(p.newsletter_opt_in),
                   (SELECT COUNT(DISTINCT customer_id) FROM newsletter_subscriptions
                    WHERE unsubscribed_at IS NULL)
            FROM customer_profiles p""",
        "promotion_profiles.is_stackable": """
            SELECT SUM(pp.is_stackable<>pr.stackable), SUM(pp.is_stackable), SUM(pr.stackable)
            FROM promotion_profiles pp JOIN promotion_rules pr ON pr.promotion_id=pp.promotion_id""",
        "promotion_profiles.min_order_amount": """
            SELECT SUM(pp.min_order_amount<>pr.min_amount),
                   SUM(pp.min_order_amount), SUM(pr.min_amount)
            FROM promotion_profiles pp JOIN promotion_rules pr ON pr.promotion_id=pp.promotion_id""",
        "promotion_rules.applies_to": """
            SELECT SUM(pr.applies_to COLLATE utf8mb4_unicode_ci <>
                       REPLACE(pp.applies_to_scope,'SKU','PRODUCT') COLLATE utf8mb4_unicode_ci),
                   NULL, NULL
            FROM promotion_rules pr JOIN promotion_profiles pp ON pp.promotion_id=pr.promotion_id""",
        "supplier_profiles.payment_terms": """
            SELECT SUM(sp.payment_terms COLLATE utf8mb4_unicode_ci <> (CASE sc.payment_terms
                     WHEN '月結30天' THEN 'NET30' WHEN '月結60天' THEN 'NET60'
                     WHEN '預付訂金' THEN 'PREPAID' WHEN '貨到付款' THEN 'COD'
                     END) COLLATE utf8mb4_unicode_ci), NULL, NULL
            FROM supplier_profiles sp JOIN supplier_contracts sc ON sc.supplier_id=sp.supplier_id""",
        "order_profiles.installment_periods": """
            SELECT SUM(NOT(op.installment_periods <=> COALESCE(pp.installment_periods,0))),
                   SUM(op.installment_periods), SUM(COALESCE(pp.installment_periods,0))
            FROM order_profiles op LEFT JOIN payments py ON py.order_id=op.order_id
            LEFT JOIN payment_profiles pp ON pp.payment_id=py.id""",
        "order_profiles.currency_code": """
            SELECT SUM(NOT(op.currency_code COLLATE utf8mb4_unicode_ci
                           <=> pp.currency_code COLLATE utf8mb4_unicode_ci)), NULL, NULL
            FROM order_profiles op JOIN payments py ON py.order_id=op.order_id
            JOIN payment_profiles pp ON pp.payment_id=py.id""",
        "order_profiles.exchange_rate": """
            SELECT SUM(NOT(op.exchange_rate <=> pp.exchange_rate)), NULL, NULL
            FROM order_profiles op JOIN payments py ON py.order_id=op.order_id
            JOIN payment_profiles pp ON pp.payment_id=py.id""",
        "invoices.carrier": """
            SELECT SUM(op.invoice_type COLLATE utf8mb4_unicode_ci
                       = 'DONATE' COLLATE utf8mb4_unicode_ci AND i.carrier IS NOT NULL),
                   SUM(i.carrier IS NOT NULL), NULL
            FROM invoices i JOIN order_profiles op ON op.order_id=i.order_id""",
        "order_profiles.invoice_carrier": """
            SELECT SUM(NOT(op.invoice_carrier COLLATE utf8mb4_unicode_ci
                           <=> i.carrier COLLATE utf8mb4_unicode_ci)),
                   SUM(op.invoice_carrier IS NOT NULL), SUM(i.carrier IS NOT NULL)
            FROM order_profiles op LEFT JOIN invoices i ON i.order_id=op.order_id""",
        "order_profiles.is_split_payment": """
            SELECT SUM(op.is_split_payment <>
                       ((SELECT COUNT(*) FROM payments y WHERE y.order_id=op.order_id)>=2)),
                   SUM(op.is_split_payment),
                   (SELECT COUNT(*) FROM (SELECT order_id FROM payments
                    GROUP BY order_id HAVING COUNT(*)>=2) t)
            FROM order_profiles op""",
        "return_profiles.received_at": """
            SELECT SUM(NOT(p.received_at <=> rs.received_at)),
                   SUM(p.received_at IS NOT NULL), SUM(rs.received_at IS NOT NULL)
            FROM return_profiles p LEFT JOIN return_shipments rs ON rs.return_id=p.return_id""",
        "return_profiles.transit_days": """
            SELECT SUM(NOT(p.transit_days <=> IF(rs.received_at IS NULL, NULL,
                       TIMESTAMPDIFF(DAY, rs.shipped_at, rs.received_at)))),
                   SUM(p.transit_days IS NOT NULL), SUM(rs.received_at IS NOT NULL)
            FROM return_profiles p LEFT JOIN return_shipments rs ON rs.return_id=p.return_id""",
        "product_specs.energy_label": """
            SELECT SUM(ps.energy_label IS NOT NULL
                       AND ps.energy_label COLLATE utf8mb4_unicode_ci
                           <> CONCAT(pp.energy_label,'級') COLLATE utf8mb4_unicode_ci),
                   SUM(ps.energy_label IS NOT NULL), NULL
            FROM product_specs ps JOIN product_profiles pp ON pp.product_id=ps.product_id""",
        "subscriptions.ended_at": """
            SELECT SUM(s.status='PAUSED' AND s.ended_at IS NOT NULL),
                   SUM(s.ended_at IS NULL), SUM(s.status<>'CANCELLED')
            FROM subscriptions s""",
        "delivery_attempts.result": """
            SELECT SUM(d.result='DELIVERED' AND s.status<>'DELIVERED'),
                   SUM(d.result<>'DELIVERED'),
                   SUM(d.result<>'DELIVERED' OR s.status<>'DELIVERED')
            FROM delivery_attempts d JOIN shipments s ON s.id=d.shipment_id""",
        "store_profiles.mon_open": """
            SELECT SUM(s.is_active=0 AND p.mon_open COLLATE utf8mb4_unicode_ci
                       <> 'CLOSED' COLLATE utf8mb4_unicode_ci), NULL, NULL
            FROM store_profiles p JOIN stores s ON s.id=p.store_id""",
        "product_profiles.package_weight_g": """
            SELECT SUM(pp.package_weight_g < ps.weight_g),
                   SUM(pp.package_weight_g), SUM(ps.weight_g)
            FROM product_profiles pp JOIN product_specs ps ON ps.product_id=pp.product_id""",
        "product_profiles.package_length_cm": """
            SELECT SUM(pp.package_length_cm*10 < ps.length_mm
                    OR pp.package_width_cm*10  < ps.width_mm
                    OR pp.package_height_cm*10 < ps.height_mm), NULL, NULL
            FROM product_profiles pp JOIN product_specs ps ON ps.product_id=pp.product_id""",
    }
    # 護欄：preview 的清單曾經與 FIXES 脫節，害 --dry-run 少報兩條。
    missing = {f"{t}.{c}" for t, c, _w, _s in FIXES} - set(q)
    assert not missing, f"FIXES 有、preview 沒有：{sorted(missing)} —— 預覽會騙人"
    print(f"{'欄位':46s} {'要改列數':>8s} {'現在合計':>10s} {'對齊後':>10s}")
    for k, sql in q.items():
        n, before, after = conn.execute(text(sql), {"cut": CUTOFF}).fetchone()
        b = "—" if before is None else f"{int(before):d}"
        a = "—" if after is None else f"{int(after):d}"
        print(f"  {k:44s} {int(n or 0):>8d} {b:>10s} {a:>10s}")


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
            n = conn.execute(text(sql), {"cut": CUTOFF}).rowcount
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
