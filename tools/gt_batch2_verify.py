# -*- coding: utf-8 -*-
"""第二批 30 題 GT（#280~#309）—— 先驗證，再進題庫

§5.1 的寫法紀律：
  · **只選問句問到的欄位**。eval_score 容忍多給、不容忍少給，
    所以 GT 越窄，可接受的模型寫法越多。
  · 「哪些／哪幾筆」才加識別欄位；識別方式有歧義的（shipment_id vs tracking_no）
    補 alt_sql，不要賭模型選哪一個。
  · 極值用子查詢，不用 ORDER BY LIMIT。
  · 平手要先查出來 —— 平手題的答案是任意的，寫了也不能用。
"""
import io
import os
import sys

_ROOT = r"C:\Text-to-SQL"
sys.path.insert(0, _ROOT)
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

SP = "shipment_profiles"
GT = [
 # ---------------- shipment_profiles ----------------
 (280, "SELECT exception_code, exception_note, attempt_count, "
       "last_scan_location, last_scan_at FROM shipment_profiles "
       "WHERE exception_code <> 'NONE'", None,
  "只選問到的五欄。exception_code = 'NONE' 是「沒有異常」的表示法，"
  "所以條件是 <> 'NONE' 而不是 IS NOT NULL。"),

 (281, "SELECT signed_by_name, signature_type, delivered_floor, needs_elevator "
       "FROM shipment_profiles WHERE recipient_relation = 'NEIGHBOR'", None,
  "「鄰居幫忙簽收」對應 recipient_relation = 'NEIGHBOR'，"
  "不是 signature_type —— 那問的是簽收方式。"),

 (282, "SELECT shipment_id, redirect_reason FROM shipment_profiles "
       "WHERE is_redirected = 1",
  "SELECT s.tracking_no, sp.redirect_reason FROM shipment_profiles sp "
  "JOIN shipments s ON s.id = sp.shipment_id WHERE sp.is_redirected = 1",
  "「哪些出貨」的識別方式有兩種合理選擇：shipment_id 或 tracking_no，"
  "所以補 alt_sql，不賭模型選哪一個。"),

 (283, "SELECT carton_type, box_length_cm, box_width_cm, box_height_cm, "
       "carton_count FROM shipment_profiles "
       "WHERE volumetric_weight_g > actual_weight_g", None,
  "兩欄相比，不是跟常數比。"),

 (284, "SELECT sp.shipment_id, sp.has_fragile_label, sp.packing_material, "
       "sp.seal_type FROM shipment_profiles sp "
       "JOIN shipments s ON s.id = sp.shipment_id "
       "JOIN order_items oi ON oi.order_id = s.order_id "
       "JOIN product_profiles pp ON pp.product_id = oi.product_id "
       "WHERE pp.is_fragile = 1 GROUP BY sp.shipment_id, sp.has_fragile_label, "
       "sp.packing_material, sp.seal_type",
  "SELECT DISTINCT sp.has_fragile_label, sp.packing_material, sp.seal_type "
  "FROM shipment_profiles sp "
  "JOIN shipments s ON s.id = sp.shipment_id "
  "JOIN order_items oi ON oi.order_id = s.order_id "
  "JOIN product_profiles pp ON pp.product_id = oi.product_id "
  "WHERE pp.is_fragile = 1",
  "雙寬表題。主 SQL 逐筆出貨（一張出貨可能含多件易碎品，所以要 GROUP BY 去重）；"
  "alt_sql 是「只看有哪幾種組合」的讀法。"
  "易碎是**商品**屬性（product_profiles），貼標籤是**出貨**屬性 —— "
  "兩者刻意互不蘊含（易碎出貨 73 筆、貼標籤 63 筆、交集 51），"
  "所以只查 has_fragile_label = 1 會得到不同答案。"),

 # ---------------- review_profiles ----------------
 (285, "SELECT hidden_reason, report_count, report_reason, moderation_status, "
       "moderator FROM review_profiles WHERE is_hidden = 1", None,
  "誘餌題：reviews.is_deleted 也有 9 則，與 is_hidden 的 8 則**零重疊**，"
  "模型選錯欄位一定答錯。"),

 (286, "SELECT incentive_type, photo_count, helpful_votes, sentiment_label "
       "FROM review_profiles WHERE is_incentivized = 1", None,
  "「拿了回饋才寫」= is_incentivized = 1。incentive_type 在沒回饋時是 'NONE'，"
  "所以不能用 incentive_type <> 'NONE' 以外的寫法當主條件（結果相同，"
  "但語意上主詞是 is_incentivized）。"),

 (287, "SELECT review_id, days_after_delivery FROM review_profiles "
       "WHERE days_after_delivery > 30",
  "SELECT COUNT(*) FROM review_profiles WHERE days_after_delivery > 30",
  "「有沒有」可以答成清單也可以答成筆數，兩種都合法，補 alt_sql。"
  "嚴格大於 30 —— 「超過三十天」不含第 30 天。"),

 (288, "SELECT title, body_length, device_type, language_code, "
       "is_verified_purchase FROM review_profiles WHERE is_featured = 1", None,
  "is_featured 與 is_pinned 是不同概念（精選 vs 置頂），別選錯。"),

 # ---------------- payment_profiles ----------------
 (289, "SELECT chargeback_at, chargeback_reason, dispute_status, "
       "dispute_closed_at FROM payment_profiles WHERE is_chargeback = 1", None,
  "5 筆全部掛在母表 status = SUCCESS 的付款上（準則 4b）。"),

 (290, "SELECT card_brand, card_issuer, card_country, currency_code "
       "FROM payment_profiles WHERE is_foreign_card = 1", None,
  "「國外的卡」是 is_foreign_card，不是 card_country <> 'TW' —— "
  "後者結果相同但那是推導，前者是登記值。"),

 (291, "SELECT p.order_id, pp.installment_periods, pp.is_zero_interest, "
       "pp.first_payment_date, pp.acquirer_name "
       "FROM payment_profiles pp JOIN payments p ON p.id = pp.payment_id "
       "WHERE pp.is_installment = 1", None,
  "問句主詞是「訂單」，所以要 join payments 拿 order_id。"),

 (292, "SELECT payment_id FROM payment_profiles WHERE is_reconciled = 0",
  "SELECT p.transaction_no FROM payment_profiles pp "
  "JOIN payments p ON p.id = pp.payment_id WHERE pp.is_reconciled = 0",
  "識別方式有歧義，補 alt_sql。"),

 (293, "SELECT pp.gateway_name, pp.response_code, pp.authorized_at, pp.captured_at "
       "FROM payment_profiles pp "
       "JOIN payments p ON p.id = pp.payment_id "
       "JOIN order_profiles op ON op.order_id = p.order_id "
       "WHERE op.device_type = 'MOBILE' AND pp.is_3ds_verified = 1", None,
  "雙寬表題，而且兩張都是寬表（order_profiles 47 欄、payment_profiles 44 欄）。"
  "手機下單 87 筆、3D 驗證 115 筆、交集 68 —— 少查任何一邊都答不對。"),

 # ---------------- support_ticket_profiles ----------------
 (294, "SELECT intake_channel, category_l1, first_response_minutes, "
       "sla_target_minutes, breach_reason FROM support_ticket_profiles "
       "WHERE is_sla_breached = 1", None,
  "is_sla_breached 是登記值，等價於 first_response_minutes > sla_target_minutes。"),

 (295, "SELECT assigned_agent, transfer_count, escalation_level, escalated_to "
       "FROM support_ticket_profiles WHERE is_supervisor_involved = 1", None,
  "5 張，全部 escalation_level >= 1（矛盾檢查保證）。"),

 (296, "SELECT ticket_id, reopened_count FROM support_ticket_profiles "
       "WHERE reopened_count > 0",
  "SELECT t.subject FROM support_ticket_profiles sp "
  "JOIN support_tickets t ON t.id = sp.ticket_id WHERE sp.reopened_count > 0",
  "識別方式有歧義（ticket_id 或工單主旨），補 alt_sql。"),

 (297, "SELECT cp.tier, sp.assigned_team, sp.resolution_type, "
       "sp.satisfaction_label FROM support_ticket_profiles sp "
       "JOIN support_tickets t ON t.id = sp.ticket_id "
       "JOIN customer_profiles cp ON cp.customer_id = t.customer_id "
       "WHERE sp.is_vip_flagged = 1", None,
  "雙寬表題，而且刻意有陷阱：問的是「**現在的**會員等級」= customer_profiles.tier"
  "（BRONZE/SILVER/GOLD/PLATINUM），"
  "不是 support_ticket_profiles.customer_tier_at_intake（受理當下的中文快照）。"
  "兩欄值域完全不同，選錯欄位一定答錯。"),

 # ---------------- subscription_profiles ----------------
 (298, "SELECT paused_at, pause_reason, resume_scheduled_at, pause_count, "
       "max_pause_days FROM subscription_profiles WHERE is_paused = 1", None,
  "5 筆，與母表 subscriptions.status = 'PAUSED' 完全一致（準則 4b）。"),

 (299, "SELECT conversion_source, trial_days, billing_cycle, monthly_amount "
       "FROM subscription_profiles WHERE converted_from_trial = 1", None,
  "轉付費 4 筆是「有試用 6 筆」的真子集 —— 用 had_trial = 1 會多兩筆。"),

 (300, "SELECT subscription_id, last_failure_reason FROM subscription_profiles "
       "WHERE failed_billing_count > 0",
  "SELECT s.plan_name, sp.last_failure_reason FROM subscription_profiles sp "
  "JOIN subscriptions s ON s.id = sp.subscription_id "
  "WHERE sp.failed_billing_count > 0",
  "識別方式有歧義，補 alt_sql。"),

 (301, "SELECT cancel_requested_at, cancel_reason, cancel_feedback, "
       "billed_count, is_win_back_targeted FROM subscription_profiles "
       "WHERE cancel_requested_at IS NOT NULL", None,
  "6 筆，與母表 status = 'CANCELLED' 一致。"
  "注意 PAUSED 的 5 筆**不算已取消** —— 那是三態裡的另一態。"),

 # ---------------- promotion_profiles ----------------
 (302, "SELECT applies_to_scope, stack_priority, excludes_coupon, "
       "excludes_gift_card FROM promotion_profiles WHERE is_stackable = 0", None,
  "只有 8 檔活動，3 檔不可疊加。"),

 (303, "SELECT promotion_id, exhausted_at, budget_amount, budget_used, used_count "
       "FROM promotion_profiles WHERE is_budget_exhausted = 1",
  "SELECT p.name, pp.exhausted_at, pp.budget_amount, pp.budget_used, pp.used_count "
  "FROM promotion_profiles pp JOIN promotions p ON p.id = pp.promotion_id "
  "WHERE pp.is_budget_exhausted = 1",
  "「哪幾檔活動」用活動名稱識別也合理，補 alt_sql。"),

 (304, "SELECT banner_headline, banner_subtext, display_slot, badge_text, "
       "landing_url FROM promotion_profiles WHERE requires_code = 1", None,
  "requires_code 與 is_auto_applied 互斥（矛盾檢查保證），所以用哪一個當條件"
  "結果相同 —— 但問句說的是「需要輸入代碼」。"),

 (305, "SELECT DISTINCT op.utm_source, pp.min_order_amount, pp.max_discount_amount "
       "FROM promotion_profiles pp "
       "JOIN order_promotions o2p ON o2p.promotion_id = pp.promotion_id "
       "JOIN order_profiles op ON op.order_id = o2p.order_id "
       "WHERE pp.display_slot = 'HOME_TOP' AND pp.is_auto_applied = 1", None,
  "雙寬表題，走 order_promotions 這條真實外鍵路徑"
  "（promotions 與 campaigns 之間沒有外鍵，所以不用「同期」那種日期比對）。"),

 # ---------------- return_profiles ----------------
 (306, "SELECT reason_l1, inspector, package_condition, missing_accessories, "
       "disposition_decision FROM return_profiles "
       "WHERE inspection_result = 'FAIL'", None,
  "7 筆，與母表 order_returns.status = 'REJECTED' 完全一致（準則 4b）—— "
  "模型用母表狀態或用這一欄都會對，這是設計要的。"),

 (307, "SELECT return_id, fault_note, carrier_name, transit_days, "
       "received_warehouse FROM return_profiles WHERE is_carrier_fault = 1",
  "SELECT rp.fault_note, rp.carrier_name, rp.transit_days, rp.received_warehouse "
  "FROM return_profiles rp WHERE rp.is_carrier_fault = 1",
  "識別欄位可有可無，補 alt_sql。"),

 (308, "SELECT return_id, requested_at, approved_at, policy_window_days "
       "FROM return_profiles "
       "WHERE is_over_policy_window = 1 AND approved_at IS NOT NULL",
  "SELECT COUNT(*) FROM return_profiles "
  "WHERE is_over_policy_window = 1 AND approved_at IS NOT NULL",
  "「最後還是讓他退了」= approved_at IS NOT NULL，兩個條件都要 —— "
  "只查超期限會多出沒核准的那些。"),

 (309, "SELECT exchange_product_id, exchange_shipped_at, compensation_type, "
       "compensation_note, is_goodwill FROM return_profiles "
       "WHERE is_exchange = 1", None,
  "4 筆，全部 exchange_product_id 非 NULL（矛盾檢查保證）。"),
]


def main():
    log.remove()
    bad = []
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        for qid, sql, alt, note in GT:
            try:
                rows = conn.execute(text(sql)).fetchall()
            except Exception as exc:
                bad.append((qid, f"主 SQL 跑不動: {type(exc).__name__}: {exc}"))
                print(f"#{qid}  ✗ {type(exc).__name__}: {str(exc)[:120]}")
                continue
            alt_n = None
            if alt:
                try:
                    alt_n = len(conn.execute(text(alt)).fetchall())
                except Exception as exc:
                    bad.append((qid, f"alt_sql 跑不動: {exc}"))
            flag = ""
            if not rows:
                flag = "  ✗ 空結果"
                bad.append((qid, "空結果"))
            sample = str(rows[0][:5]) if rows else ""
            print(f"#{qid}  {len(rows):4d} 列"
                  f"{f' / alt {alt_n}' if alt_n is not None else '':>12}"
                  f"{flag}   {sample[:78]}")
    print()
    if bad:
        print(f"{len(bad)} 題有問題：")
        for qid, why in bad:
            print(f"  #{qid} {why}")
        return 1
    print(f"{len(GT)} 題全部跑得動且非空。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
