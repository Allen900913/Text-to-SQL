"""第二批寬表建表器 —— 86 → 93 張表（只 CREATE + INSERT）

與 add_wide_tables.py 的關係：**共用純函式，不共用亂數源。**

    build_ddl / fingerprints / _norm   直接 import（沒有隨機性，共用是安全的）
    _pick / _dt                        **重寫**。第一批那兩支綁在模組層的
                                       R = RNG_WIDE 上，import 過來會從第一批的
                                       序列抽數 —— 硬約束第 2 條要的是專屬種子。

為什麼這批要「刻意重疊」，以及每張表跟誰搶題，寫在 wide_table_plan_2.yaml 的
重疊地圖裡，不在這裡重複。

用法：
    python tools/add_wide_tables_2.py            # 乾跑，只印 DDL
    python tools/add_wide_tables_2.py --apply    # 實際建表並灌資料
    python tools/add_wide_tables_2.py --drop     # 還原這七張表
"""
import argparse
import io
import os
import random
import sys
from datetime import date, datetime, timedelta

import yaml
from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from add_wide_tables import build_ddl, fingerprints  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN_PATH = os.path.join(ROOT, "tools", "wide_table_plan_2.yaml")

# 第二批的專屬亂數源。灌過之後就不要再動 —— 30 題 GT 會綁在這份資料上。
RNG_WIDE2 = random.Random(20260825)
R = RNG_WIDE2

TODAY = date(2026, 8, 25)


def _pick(seq, weights=None):
    return R.choices(seq, weights=weights, k=1)[0]


def _as_dt(v) -> datetime:
    """母表欄位可能是 date 也可能是 datetime，統一成 datetime 再算偏移。"""
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, 9, 0)
    return datetime(2026, 1, 1, 9, 0)


def _after(base, lo_min: int, hi_min: int) -> datetime:
    """在 base 之後的 lo~hi 分鐘。用分鐘而不是天，時效類欄位才有解析度。"""
    return _as_dt(base) + timedelta(minutes=R.randint(lo_min, hi_min))


def _名(prefix: str) -> str:
    surnames = "陳林黃張李王吳劉蔡楊許鄭謝洪郭邱曾廖賴徐"
    givens = ["家豪", "怡君", "志明", "淑芬", "建宏", "美玲", "俊傑", "雅婷",
              "冠宇", "宜蓁", "承翰", "詩涵", "柏翰", "欣怡"]
    return f"{prefix}{R.choice(surnames)}{R.choice(givens)}" if prefix else \
        f"{R.choice(surnames)}{R.choice(givens)}"


# ===========================================================================
# 1. shipment_profiles —— 掛在 shipments（143 列）
# ===========================================================================
_HUBS = ["台北轉運中心", "桃園集貨站", "新竹轉運站", "台中轉運中心",
         "嘉義集貨站", "台南轉運站", "高雄轉運中心", "宜蘭集貨站"]


def gen_shipment_profile(sid: int, shipped_at, delivered_at, status) -> dict:
    base = _as_dt(shipped_at or date(2026, 1, 1))
    cartons = _pick([1, 1, 1, 2, 3], None)
    L, W, H = (R.choice([20, 25, 30, 35, 40, 45, 60]),
               R.choice([15, 20, 25, 30, 35]),
               R.choice([10, 12, 15, 20, 25, 30]))
    # 材積重 = 長x寬x高 / 6000 x 1000 公克（業界慣例的 6000 除數）
    vol = int(L * W * H / 6000 * 1000)
    actual = int(vol * R.uniform(0.5, 1.6))
    hubs = R.sample(_HUBS, k=R.choice([1, 1, 2, 3]))
    exc = _pick(["NONE", "NONE", "NONE", "NONE", "ADDR", "ABSENT",
                 "REFUSE", "DAMAGE", "DELAY"])
    redirected = 1 if exc == "ADDR" and R.random() < 0.6 else 0
    damaged = 1 if exc == "DAMAGE" else 0
    relation = _pick(["SELF", "SELF", "SELF", "FAMILY", "NEIGHBOR",
                      "GUARD", "COWORKER"])
    sig = _pick(["SIGN", "SIGN", "STAMP", "PHOTO", "NONE"])
    zone = _pick(["AMBIENT", "AMBIENT", "AMBIENT", "AMBIENT", "CHILL", "FROZEN"])
    cold = 1 if zone in ("CHILL", "FROZEN") else 0
    attempts = 1 if exc == "NONE" else R.randint(2, 4)
    return {
        "shipment_id": sid,
        "carton_type": _pick(["BOX", "BOX", "BOX", "BAG", "TUBE", "PALLET"]),
        "carton_count": cartons,
        "box_length_cm": L, "box_width_cm": W, "box_height_cm": H,
        "volumetric_weight_g": vol, "actual_weight_g": actual,
        "is_repacked": 1 if R.random() < 0.18 else 0,
        "packing_material": _pick(["AIR", "AIR", "FOAM", "PAPER", "NONE"]),
        "has_fragile_label": 0,          # guarantee() 依商品易碎屬性覆寫
        "seal_type": _pick(["TAPE", "TAPE", "STRAP", "SEAL"]),
        "origin_hub": hubs[0],
        "transit_hub_1": hubs[1] if len(hubs) > 1 else None,
        "transit_hub_2": hubs[2] if len(hubs) > 2 else None,
        "hub_count": len(hubs),
        "route_code": f"RT-{R.randint(10, 99)}",
        "last_scan_location": hubs[-1] if status != "DELIVERED" else "配送車輛",
        "last_scan_at": _after(base, 60, 4320),
        "is_cross_city": 1 if len(hubs) > 1 else 0,
        "distance_band": _pick(["SHORT", "MID", "MID", "LONG", "REMOTE"]),
        "delivery_window": _pick(["ANY", "ANY", "MORNING", "AFTERNOON", "NIGHT"]),
        "recipient_relation": relation,
        "signature_type": sig,
        "signed_by_name": None if sig == "NONE" else _名(""),
        "id_check_required": 1 if R.random() < 0.12 else 0,
        "is_contactless": 1 if sig == "NONE" else 0,
        "photo_proof_count": R.randint(0, 3),
        "delivered_floor": f"{R.randint(1, 14)}F",
        "needs_elevator": 1 if R.random() < 0.35 else 0,
        "attempt_count": attempts,
        "exception_code": exc,
        "exception_note": None if exc == "NONE" else {
            "ADDR": "門牌號碼與系統登記不符，聯繫收件人確認",
            "ABSENT": "按鈴無人回應，留置招領通知單",
            "REFUSE": "收件人表示未訂購，當場拒收",
            "DAMAGE": "外箱於轉運過程受擠壓",
            "DELAY": "颱風警報停止配送作業",
        }[exc],
        "is_redirected": redirected,
        "redirect_reason": "收件人要求改送公司地址" if redirected else None,
        "returned_to_hub_at": _after(base, 2880, 7200) if exc == "REFUSE" else None,
        "damage_reported": damaged,
        "damage_note": "外箱凹陷，內容物經確認完好" if damaged else None,
        "temperature_zone": zone,
        "needs_cold_chain": cold,
        "dry_ice_used": 1 if zone == "FROZEN" else 0,
        "is_dangerous_goods_declared": 1 if R.random() < 0.06 else 0,
        "handling_note": _pick([None, None, None, "請勿倒置", "請輕放",
                                "送達前請先致電", "請置於管理室"]),
        "created_at": base,
    }


# ===========================================================================
# 2. review_profiles —— 掛在 reviews（134 列）
# ===========================================================================
def gen_review_profile(rid: int, created_at, is_deleted) -> dict:
    base = _as_dt(created_at)
    edited = 1 if R.random() < 0.22 else 0
    photos = _pick([0, 0, 0, 1, 2, 3, 4])
    reports = _pick([0, 0, 0, 0, 0, 1, 2, 3])
    reason = "NONE" if reports == 0 else _pick(["SPAM", "ABUSE", "OFFTOPIC", "FAKE"])
    mod = _pick(["APPROVED", "APPROVED", "APPROVED", "APPROVED",
                 "PENDING", "REJECTED"])
    incent = 1 if R.random() < 0.2 else 0
    lang = _pick(["zh-TW", "zh-TW", "zh-TW", "zh-TW", "en", "ja"])
    merchant = 1 if R.random() < 0.4 else 0
    return {
        "review_id": rid,
        "title": _pick(["整體來說很滿意", "跟想像中有落差", "回購第三次了",
                        "包裝可以再加強", "出貨很快", "CP 值不錯",
                        "客服處理得很好", "尺寸偏小要注意"]),
        "body_length": R.randint(12, 480),
        "has_photo": 1 if photos else 0,
        "photo_count": photos,
        "has_video": 1 if R.random() < 0.08 else 0,
        "is_edited": edited,
        "edit_count": R.randint(1, 3) if edited else 0,
        "first_posted_at": base,
        "last_edited_at": _after(base, 1440, 40320) if edited else None,
        "device_type": _pick(["MOBILE", "MOBILE", "MOBILE", "DESKTOP", "TABLET"]),
        "input_channel": _pick(["APP", "APP", "WEB", "EMAIL"]),
        "is_verified_purchase": 1 if R.random() < 0.88 else 0,
        "days_after_delivery": _pick([1, 2, 3, 4, 5, 7, 9, 12, 15, 20, 26,
                                      31, 34, 40, 45]),
        "reviewer_review_count": R.randint(1, 9),
        "is_first_review_by_user": 1 if R.random() < 0.3 else 0,
        "is_incentivized": incent,
        "incentive_type": _pick(["POINTS", "COUPON"]) if incent else "NONE",
        "helpful_votes": R.randint(0, 42),
        "unhelpful_votes": R.randint(0, 9),
        "reply_count": R.randint(1, 3) if merchant else 0,
        "has_merchant_reply": merchant,
        "merchant_reply_at": _after(base, 120, 10080) if merchant else None,
        "report_count": reports,
        "report_reason": reason,
        "moderation_status": mod,
        "moderated_at": None if mod == "PENDING" else _after(base, 30, 2880),
        "moderator": None if mod == "PENDING" else _名("審核-"),
        "is_hidden": 0,                  # guarantee() 指定集合，且刻意與 is_deleted 不同
        "hidden_reason": None,
        "is_pinned": 1 if R.random() < 0.07 else 0,
        "is_featured": 0,                # guarantee() 指定集合
        "language_code": lang,
        "is_translated": 1 if lang != "zh-TW" else 0,
        "translated_from": lang if lang != "zh-TW" else None,
        "sentiment_label": _pick(["POSITIVE", "POSITIVE", "POSITIVE",
                                  "NEUTRAL", "NEGATIVE"]),
        "topic_tags": ";".join(R.sample(
            ["外觀", "耐用度", "尺寸", "價格", "包裝", "到貨速度", "客服",
             "說明書", "配件"], k=R.randint(1, 3))),
        "mentions_speed": 1 if R.random() < 0.35 else 0,
        "mentions_packaging": 1 if R.random() < 0.3 else 0,
        "mentions_service": 1 if R.random() < 0.22 else 0,
        "created_at": base,
    }


# ===========================================================================
# 3. payment_profiles —— 掛在 payments（164 列）
# ===========================================================================
_ISSUERS = ["國泰世華", "中國信託", "玉山銀行", "台新銀行", "富邦銀行",
            "第一銀行", "花旗銀行", "星展銀行"]


def gen_payment_profile(pid: int, paid_at, status, amount) -> dict:
    base = _as_dt(paid_at or date(2026, 1, 1))
    ok = status == "SUCCESS"
    foreign = 1 if R.random() < 0.11 else 0
    inst = 1 if R.random() < 0.24 else 0
    periods = _pick([3, 6, 12, 24]) if inst else 0
    wallet = _pick(["NONE", "NONE", "NONE", "APPLE", "GOOGLE", "LINE"])
    cb = 0                                # guarantee() 指定集合
    disp = "NONE"
    recon = 1 if (ok and R.random() < 0.8) else 0
    cur = "USD" if foreign and R.random() < 0.5 else "TWD"
    rate = round(R.uniform(29.5, 32.4), 4) if cur != "TWD" else 1.0
    return {
        "payment_id": pid,
        "gateway_name": _pick(["綠界科技", "藍新金流", "PayPal", "Stripe"]),
        "gateway_reference": f"GW{R.randint(10**9, 10**10 - 1)}",
        "acquirer_name": _pick(["聯合信用卡中心", "台灣票據交換所", "NCCC"]),
        "authorization_code": f"{R.randint(100000, 999999)}",
        "authorized_at": base,
        "captured_at": _after(base, 5, 2880) if ok else None,
        "response_code": "00" if ok else _pick(["05", "51", "54", "61", "91"]),
        "response_message": "核准" if ok else _pick(
            ["發卡行拒絕授權", "餘額不足", "卡片已過期", "超過單筆限額",
             "發卡行系統無回應"]),
        "is_3ds_verified": 1 if R.random() < 0.62 else 0,
        "threeds_version": _pick(["2.1", "2.2", "1.0"]),
        "avs_result": _pick(["Y", "Y", "A", "N", "U"]),
        "card_brand": _pick(["VISA", "VISA", "MASTER", "MASTER", "JCB", "AMEX"]),
        "card_bin": f"{R.randint(400000, 559999)}",
        "card_last4": f"{R.randint(0, 9999):04d}",
        "card_country": "JP" if foreign and R.random() < 0.4 else (
            "US" if foreign else "TW"),
        "card_issuer": R.choice(["三井住友", "MUFG", "Chase", "Citi"]) if foreign
                       else R.choice(_ISSUERS),
        "is_foreign_card": foreign,
        "card_expiry_month": R.randint(1, 12),
        "card_expiry_year": R.randint(2026, 2031),
        "is_tokenized": 1 if wallet != "NONE" or R.random() < 0.3 else 0,
        "wallet_type": wallet,
        "is_installment": inst,
        "installment_periods": periods,
        "is_zero_interest": 1 if inst and R.random() < 0.55 else 0,
        "first_payment_date": (base + timedelta(days=R.randint(20, 45))).date()
                              if inst else None,
        "bonus_points_used": _pick([0, 0, 0, 0, 50, 100, 200, 500]),
        "gift_card_applied": f"GC{R.randint(100000, 999999)}"
                             if R.random() < 0.09 else None,
        "coupon_code_applied": _pick([None, None, None, None,
                                      "WELCOME100", "SUMMER20", "VIP500"]),
        "is_reconciled": recon,
        "reconciled_at": _after(base, 1440, 20160) if recon else None,
        "statement_batch": f"ST-2026{R.randint(1, 12):02d}" if recon else None,
        "settlement_date": (base + timedelta(days=R.randint(2, 9))).date()
                           if ok else None,
        "currency_code": cur,
        "exchange_rate": rate,
        "original_currency_amount": round(float(amount or 0) / rate, 2),
        "is_chargeback": cb,
        "chargeback_at": None,
        "chargeback_reason": None,
        "dispute_status": disp,
        "dispute_closed_at": None,
        "void_at": _after(base, 60, 1440) if (not ok and R.random() < 0.4) else None,
        "created_at": base,
    }


# ===========================================================================
# 4. support_ticket_profiles —— 掛在 support_tickets（36 列）
# ===========================================================================
_TEAMS = ["第一線客服", "訂單處理組", "退換貨組", "技術支援組", "帳務組"]


def gen_support_ticket_profile(tid: int, created_at, status, order_id) -> dict:
    base = _as_dt(created_at)
    closed = status in ("CLOSED", "RESOLVED")
    l1 = _pick(["訂單問題", "退換貨", "商品諮詢", "帳務問題", "系統操作"])
    sla = _pick([60, 120, 240, 480])
    first = R.randint(5, 600)
    return {
        "ticket_id": tid,
        "intake_channel": _pick(["PHONE", "EMAIL", "CHAT", "CHAT", "APP", "STORE"]),
        "intake_language": _pick(["zh-TW", "zh-TW", "zh-TW", "en"]),
        "category_l1": l1,
        "category_l2": {
            "訂單問題": _pick(["訂單未成立", "出貨進度", "地址修改"]),
            "退換貨": _pick(["退貨申請", "換貨進度", "退款查詢"]),
            "商品諮詢": _pick(["規格詢問", "庫存詢問", "保固範圍"]),
            "帳務問題": _pick(["發票開立", "刷卡失敗", "分期問題"]),
            "系統操作": _pick(["登入異常", "優惠券無法使用", "App 閃退"]),
        }[l1],
        "subject_line": f"[{l1}] 客戶來信詢問",
        "is_auto_classified": 1 if R.random() < 0.6 else 0,
        "related_order_id": order_id,
        "related_product_id": None,
        "is_order_related": 1 if order_id else 0,
        "first_response_at": _after(base, first, first),
        "first_response_minutes": first,
        "resolution_minutes": R.randint(first, first + 4000) if closed else None,
        "sla_target_minutes": sla,
        "is_sla_breached": 1 if first > sla else 0,
        "breach_reason": _pick(["來件量高於預期", "需等待其他部門回覆",
                                "假日人力不足"]) if first > sla else None,
        "business_hours_only": 1 if l1 in ("帳務問題", "系統操作") else 0,
        "reopened_count": 0,              # guarantee() 指定集合
        "last_reopened_at": None,
        "assigned_team": _pick(_TEAMS),
        "assigned_agent": _名("客服-"),
        "transfer_count": _pick([0, 0, 0, 1, 1, 2]),
        "escalation_level": 0,            # guarantee() 指定集合
        "escalated_at": None,
        "escalated_to": None,
        "is_supervisor_involved": 0,      # guarantee() 指定集合
        "customer_tier_at_intake": _pick(["一般會員", "銀卡會員", "金卡會員",
                                          "白金會員"]),
        "is_repeat_issue": 1 if R.random() < 0.25 else 0,
        "prior_ticket_count": R.randint(0, 5),
        "sentiment_at_intake": _pick(["CALM", "CALM", "CALM", "UPSET", "ANGRY"]),
        "is_vip_flagged": 0,              # guarantee() 指定集合
        "contact_attempts": R.randint(1, 4),
        "preferred_callback_window": _pick(["ANY", "ANY", "MORNING",
                                            "AFTERNOON", "NIGHT"]),
        "resolution_type": _pick(["ANSWERED", "ANSWERED", "REFUND", "REPLACE",
                                  "ESCALATED", "NO_RESPONSE"]) if closed else None,
        "resolution_note": "已向客戶說明並取得同意" if closed else None,
        "closed_at": _after(base, 120, 20160) if closed else None,
        "closed_by": _名("客服-") if closed else None,
        "survey_sent_at": _after(base, 200, 21000) if closed else None,
        "survey_responded": 1 if closed and R.random() < 0.45 else 0,
        "satisfaction_label": None,       # guarantee() 依 survey_responded 補
        "knowledge_article_id": None,     # guarantee() 依 faq_articles 補
        "is_knowledge_gap": 1 if R.random() < 0.2 else 0,
        "created_at": base,
    }


# ===========================================================================
# 5. subscription_profiles —— 掛在 subscriptions（17 列，小表）
# ===========================================================================
_PLANS = ["基礎方案", "標準方案", "進階方案", "尊榮方案"]


def gen_subscription_profile(sid: int, started_at, ended_at, status) -> dict:
    base = _as_dt(started_at)
    cycle = _pick(["MONTHLY", "MONTHLY", "QUARTERLY", "ANNUAL"])
    per_month = _pick([199, 299, 399, 499, 699, 999])
    billed = R.randint(1, 18)
    return {
        "subscription_id": sid,
        "billing_cycle": cycle,
        "billing_day": base.day if base.day <= 28 else 28,
        "next_billing_date": (TODAY + timedelta(days=R.randint(1, 30))),
        "last_billed_at": _after(base, 43200, 200000),
        "billed_count": billed,
        "monthly_amount": per_month,
        "total_billed_amount": per_month * billed,
        "payment_method_name": _pick(["信用卡", "信用卡", "電子錢包", "銀行轉帳"]),
        "is_auto_renew": 1,               # guarantee() 依母表狀態覆寫
        "auto_renew_changed_at": None,
        "failed_billing_count": 0,        # guarantee() 指定集合
        "last_failure_reason": None,
        "grace_period_days": _pick([3, 7, 7, 14]),
        "had_trial": 0,                   # guarantee() 指定集合
        "trial_days": 0,
        "trial_started_at": None,
        "trial_ended_at": None,
        "converted_from_trial": 0,
        "conversion_source": None,
        "is_paused": 0,                   # guarantee() 指定集合
        "paused_at": None,
        "resume_scheduled_at": None,
        "pause_count": 0,
        "pause_reason": None,
        "max_pause_days": _pick([30, 60, 90]),
        "plan_changed_count": 0,          # guarantee() 指定集合
        "previous_plan_name": None,
        "plan_changed_at": None,
        "upgrade_count": 0,
        "downgrade_count": 0,
        "is_annual_prepaid": 1 if cycle == "ANNUAL" else 0,
        "contract_end_date": (base + timedelta(days=365)).date(),
        "early_termination_note": None,
        "renewal_reminder_days": _pick([3, 7, 14]),
        "notify_by_email": 1 if R.random() < 0.85 else 0,
        "notify_by_sms": 1 if R.random() < 0.4 else 0,
        "notify_by_push": 1 if R.random() < 0.6 else 0,
        "last_reminder_sent_at": _after(base, 50000, 200000),
        "opt_out_marketing": 1 if R.random() < 0.3 else 0,
        "cancel_requested_at": None,      # guarantee() 依母表狀態
        "cancel_reason": None,
        "cancel_feedback": None,
        "is_win_back_targeted": 0,
        "created_at": base,
    }


# ===========================================================================
# 6. promotion_profiles —— 掛在 promotions（8 列，最小的表）
# ===========================================================================
def gen_promotion_profile(pid: int, starts_at, ends_at, is_active) -> dict:
    base = _as_dt(starts_at)
    scope = _pick(["ALL", "CATEGORY", "CATEGORY", "SKU", "BRAND"])
    return {
        "promotion_id": pid,
        "applies_to_scope": scope,
        "included_category": None if scope == "ALL" else _pick(
            ["3C", "家電", "服飾", "家居", "運動"]),
        "excluded_category": _pick([None, None, "菸酒", "生鮮"]),
        "included_brand": _pick([None, None, "Apple", "Sony", "Nike"]),
        "excluded_sku_list": _pick([None, None, "1001,1002", "1015"]),
        "min_quantity": _pick([1, 1, 2, 3]),
        "min_order_amount": _pick([0, 500, 1000, 1500, 3000]),
        "max_discount_amount": _pick([200, 500, 1000, 2000]),
        "first_order_only": 0,            # guarantee() 指定集合
        "member_tier_required": _pick(["NONE", "NONE", "銀卡會員", "金卡會員"]),
        "new_customer_only": 0,
        "region_limit": _pick([None, None, None, "本島"]),
        "is_stackable": 1,                # guarantee() 指定集合
        "stack_priority": R.randint(1, 5),
        "conflicts_with": None,
        "excludes_coupon": 0,
        "excludes_gift_card": 0,
        "is_exclusive": 0,
        "budget_amount": _pick([50000, 100000, 200000, 500000]),
        "budget_used": 0,                 # guarantee() 指定集合
        "usage_limit_total": _pick([100, 500, 1000, 5000]),
        "usage_limit_per_customer": _pick([1, 1, 2, 3]),
        "used_count": R.randint(3, 80),
        "is_budget_exhausted": 0,         # guarantee() 指定集合
        "exhausted_at": None,
        "banner_headline": _pick(["限時下殺", "週年慶開跑", "會員專屬回饋",
                                  "換季出清", "買越多省越多"]),
        "banner_subtext": "活動期間內單筆訂單符合門檻即可享有折扣，詳情見活動頁",
        "display_slot": _pick(["HOME_TOP", "CATEGORY", "CART", "CHECKOUT"]),
        "show_countdown": 1 if R.random() < 0.5 else 0,
        "badge_text": _pick(["限時", "熱賣", "新品", "最後一天"]),
        "landing_url": f"https://shop.example.com/promo/{pid}",
        "is_auto_applied": 1,             # guarantee() 指定集合
        "requires_code": 0,               # guarantee() 指定集合
        "created_by": _名("行銷-"),
        "approved_by": _名("主管-"),
        "approved_at": _after(base, -20160, -1440),
        "approval_note": "預算與毛利試算已確認，同意執行",
        "is_paused": 0,                   # guarantee() 依母表 is_active
        "paused_at": None,
        "pause_reason": None,
        "review_cycle": _pick(["DAILY", "WEEKLY", "WEEKLY", "END"]),
        "created_at": base,
    }


# ===========================================================================
# 7. return_profiles —— 掛在 order_returns（18 列）
# ===========================================================================
def gen_return_profile(rid: int, requested_at, status) -> dict:
    base = _as_dt(requested_at)
    r1 = _pick(["QUALITY", "WRONG_ITEM", "NOT_AS_DESCRIBED", "CHANGE_MIND",
                "DAMAGED"])
    quality = 1 if r1 in ("QUALITY", "DAMAGED") else 0
    # order_returns.status 實際只有三種：RECEIVED / REJECTED / REQUESTED。
    # 一開始寫成 COMPLETED/APPROVED/DONE，結果 18 筆全部沒有檢驗紀錄，
    # 準則檢查直接擋下來 —— 母表的值域要用查的，不能用猜的。
    inspected = status in ("RECEIVED", "REJECTED")     # 到倉才驗得了
    approved = _after(base, 1440, 10080) if status == "RECEIVED" else None
    completed = _after(base, 10080, 40320) if status == "RECEIVED" else None
    done = inspected
    return {
        "return_id": rid,
        "reason_l1": r1,
        "reason_l2": {
            "QUALITY": _pick(["功能異常", "外觀刮傷", "零件短缺"]),
            "WRONG_ITEM": _pick(["顏色不符", "尺寸不符", "寄錯商品"]),
            "NOT_AS_DESCRIBED": _pick(["材質與說明不符", "規格與頁面不同"]),
            "CHANGE_MIND": _pick(["買重複了", "臨時不需要", "找到更合適的"]),
            "DAMAGED": _pick(["外箱破損", "運送中碰撞"]),
        }[r1],
        "customer_note": "商品收到後發現與預期有落差，希望辦理退貨",
        "is_customer_fault": 1 if r1 == "CHANGE_MIND" else 0,
        "is_seller_fault": 1 if r1 in ("QUALITY", "WRONG_ITEM",
                                       "NOT_AS_DESCRIBED") else 0,
        "is_carrier_fault": 0,            # guarantee() 指定集合
        "fault_note": None,
        "is_quality_issue": quality,
        "defect_code": f"DF-{R.randint(10, 99)}" if quality else "NONE",
        "inspected_at": _after(base, 4320, 20160) if done else None,
        "inspector": _名("驗貨-") if done else None,
        # REJECTED 就是「驗過但不符合退貨條件」—— 這個對應是母表給的，不是我指定的
        "inspection_result": ("FAIL" if status == "REJECTED" else
                              "PASS" if inspected else None),
        "is_resellable": 1 if (status == "RECEIVED" and r1 == "CHANGE_MIND") else 0,
        "disposition_decision": _pick(["RESHELF", "REFURBISH", "SCRAP",
                                       "RETURN_SUPPLIER"]) if done else None,
        "package_condition": _pick(["INTACT", "INTACT", "OPENED", "BROKEN",
                                    "MISSING"]),
        "missing_accessories": _pick([None, None, None, "電源線", "說明書、保固卡"]),
        "has_original_box": 1 if R.random() < 0.75 else 0,
        "photo_count": R.randint(0, 4),
        "pickup_method": _pick(["HOME_PICKUP", "HOME_PICKUP", "STORE_DROP",
                                "SELF_SEND"]),
        "pickup_scheduled_at": _after(base, 1440, 7200),
        "carrier_name": _pick(["黑貓宅急便", "新竹物流", "宅配通", "郵局"]),
        "received_at": _after(base, 4320, 20160) if done else None,
        "received_warehouse": _pick(["北區倉", "中區倉", "南區倉"]) if done else None,
        "transit_days": R.randint(1, 6) if done else None,
        "is_exchange": 0,                 # guarantee() 指定集合
        "exchange_product_id": None,
        "exchange_shipped_at": None,
        "compensation_type": "NONE",      # guarantee() 部分覆寫
        "compensation_note": None,
        "is_goodwill": 0,
        "requested_at": base,
        "approved_at": approved,
        "approval_days": (approved - base).days if approved else None,
        "completed_at": completed,
        "total_days": (completed - base).days if completed else None,
        "is_over_policy_window": 0,       # guarantee() 指定集合
        "policy_window_days": _pick([7, 7, 14, 30]),
        "escalated": 1 if R.random() < 0.15 else 0,
        "handled_by": _名("退貨-"),
        "handling_note": "已依退貨政策完成審核與後續處理",
        "created_at": base,
    }


GENERATORS = {
    "shipment_profiles":       ("shipments",       gen_shipment_profile),
    "review_profiles":         ("reviews",         gen_review_profile),
    "payment_profiles":        ("payments",        gen_payment_profile),
    "support_ticket_profiles": ("support_tickets", gen_support_ticket_profile),
    "subscription_profiles":   ("subscriptions",   gen_subscription_profile),
    "promotion_profiles":      ("promotions",      gen_promotion_profile),
    "return_profiles":         ("order_returns",   gen_return_profile),
}

PARENT_EXTRA = {
    "shipment_profiles":       "shipped_at, delivered_at, status",
    "review_profiles":         "created_at, is_deleted",
    "payment_profiles":        "paid_at, status, amount",
    "support_ticket_profiles": "created_at, status, order_id",
    "subscription_profiles":   "started_at, ended_at, status",
    "promotion_profiles":      "starts_at, ends_at, is_active",
    "return_profiles":         "requested_at, status",
}

FK_OF = {t: yaml_fk for t, yaml_fk in [
    ("shipment_profiles", "shipment_id"), ("review_profiles", "review_id"),
    ("payment_profiles", "payment_id"), ("support_ticket_profiles", "ticket_id"),
    ("subscription_profiles", "subscription_id"),
    ("promotion_profiles", "promotion_id"), ("return_profiles", "return_id"),
]}


# ===========================================================================
# 保證式配置
# ===========================================================================
# 小表不能靠機率：promotions 只有 8 列，「命中率 20%」可能是 0 列也可能是 4 列。
# 這裡把每一題要問的條件從**機率**改成**指定集合**，並且同時滿足
# §7.4 的鑑別力準則：非空、涵蓋率 <= 75%、two_wide 的交集是兩邊的真子集。

CEIL = 0.75


def guarantee(conn, data: dict[str, list[dict]]) -> list[str]:
    out = []
    idx = {t: {r[FK_OF[t]]: r for r in rows} for t, rows in data.items()}

    def q(sql):
        return conn.execute(text(sql)).fetchall()

    # ---- shipment_profiles：易碎商品的出貨要貼標籤（two_wide 題的靶）----
    frag = {r[0] for r in q("""
        SELECT DISTINCT s.id FROM shipments s
        JOIN order_items oi ON oi.order_id = s.order_id
        JOIN product_profiles pp ON pp.product_id = oi.product_id
        WHERE pp.is_fragile = 1""")}
    ship_all = set(idx["shipment_profiles"])
    for sid in sorted(ship_all):
        idx["shipment_profiles"][sid]["has_fragile_label"] = 0
    # 兩個方向都要漏，這題才真的需要兩張表：
    #   只貼 70% 的易碎品  -> 「易碎」不蘊含「有貼」
    #   另外貼一些非易碎品 -> 「有貼」不蘊含「易碎」（現場包裝人員本來就會多貼）
    # 少了第二個方向，貼標籤就是易碎的子集，模型只查 has_fragile_label = 1
    # 就會得到一模一樣的答案 —— two_wide 題退化成單表題。
    labeled = sorted(frag)[: max(2, int(len(frag) * 0.7))]
    over = sorted(ship_all - frag)[:12]
    for sid in labeled + over:
        idx["shipment_profiles"][sid]["has_fragile_label"] = 1
    out.append(f"易碎商品出貨 {len(frag)} 筆、貼了標籤 {len(labeled) + len(over)} 筆、"
               f"交集 {len(labeled)}（兩邊互不蘊含，全 {len(ship_all)} 筆）")

    # 鄰居簽收：至少 4 筆、不超過 75%
    nb = [s for s, r in idx["shipment_profiles"].items()
          if r["recipient_relation"] == "NEIGHBOR"]
    if len(nb) < 4:
        for sid in sorted(ship_all - set(nb))[:4 - len(nb)]:
            idx["shipment_profiles"][sid]["recipient_relation"] = "NEIGHBOR"
            nb.append(sid)
    out.append(f"鄰居代簽 {len(nb)} 筆")

    # 中途改址：至少 3 筆
    rd = [s for s, r in idx["shipment_profiles"].items() if r["is_redirected"]]
    if len(rd) < 3:
        for sid in sorted(ship_all - set(rd))[:3 - len(rd)]:
            r = idx["shipment_profiles"][sid]
            r["is_redirected"] = 1
            r["redirect_reason"] = "收件人要求改送公司地址"
            rd.append(sid)
    out.append(f"中途改址 {len(rd)} 筆")

    # ---- review_profiles：is_hidden 必須與 reviews.is_deleted 是不同集合 ----
    deleted = {r[0] for r in q("SELECT id FROM reviews WHERE is_deleted = 1")}
    alive = sorted(set(idx["review_profiles"]) - deleted)
    hidden = alive[:8]                    # 全部取自「沒被刪除」的評價
    for rid in hidden:
        r = idx["review_profiles"][rid]
        r["is_hidden"] = 1
        r["hidden_reason"] = _pick(["內容涉及人身攻擊", "疑似廣告內容",
                                    "與商品無關", "重複張貼"])
    out.append(f"隱藏評價 {len(hidden)} 則，與 reviews.is_deleted 的 "
               f"{len(deleted)} 則**完全不重疊**（誘餌題要的就是這個差別）")

    featured = alive[10:16]
    for rid in featured:
        idx["review_profiles"][rid]["is_featured"] = 1
    out.append(f"精選評價 {len(featured)} 則")

    # ---- payment_profiles：退刷是指定集合（母表要是 SUCCESS）----
    succ = [r[0] for r in q("SELECT id FROM payments WHERE status = 'SUCCESS' ORDER BY id")]
    cb = succ[:5]
    for pid in cb:
        r = idx["payment_profiles"][pid]
        r["is_chargeback"] = 1
        r["chargeback_at"] = _after(r["authorized_at"], 20160, 60480)
        r["chargeback_reason"] = _pick(["持卡人否認交易", "商品未收到",
                                        "重複扣款", "金額不符"])
        r["dispute_status"] = _pick(["OPEN", "WON", "LOST"])
        if r["dispute_status"] != "OPEN":
            r["dispute_closed_at"] = _after(r["chargeback_at"], 10080, 40320)
    out.append(f"退刷 {len(cb)} 筆（全部取自母表 status=SUCCESS，準則 4b）")

    # two_wide：手機下單 x 通過 3D 驗證，交集要是兩邊的真子集
    mobile = {r[0] for r in q("""
        SELECT p.id FROM payments p
        JOIN order_profiles op ON op.order_id = p.order_id
        WHERE op.device_type = 'MOBILE'""")}
    for pid in sorted(mobile)[:6]:
        idx["payment_profiles"][pid]["is_3ds_verified"] = 1
    for pid in sorted(mobile)[6:9]:
        idx["payment_profiles"][pid]["is_3ds_verified"] = 0
    tds = {p for p, r in idx["payment_profiles"].items() if r["is_3ds_verified"]}
    out.append(f"手機下單 {len(mobile)} 筆、3D 驗證 {len(tds)} 筆、"
               f"交集 {len(mobile & tds)}（兩邊都是真子集）")

    # ---- support_ticket_profiles（36 列，全部指定）----
    tick = sorted(idx["support_ticket_profiles"])
    for i, tid in enumerate(tick):
        r = idx["support_ticket_profiles"][tid]
        if i < 5:                          # 主管介入
            r["is_supervisor_involved"] = 1
            r["escalation_level"] = R.randint(1, 2)
            r["escalated_at"] = _after(r["created_at"], 240, 2880)
            r["escalated_to"] = _名("主管-")
            r["transfer_count"] = max(1, r["transfer_count"])
        if 5 <= i < 9:                     # 結案後重開
            r["reopened_count"] = R.randint(1, 2)
            r["last_reopened_at"] = _after(r["created_at"], 4320, 30240)
        if 9 <= i < 14:                    # 優先處理
            r["is_vip_flagged"] = 1
            r["customer_tier_at_intake"] = ["金卡會員", "白金會員", "金卡會員",
                                            "銀卡會員", "白金會員"][i - 9]
    # support_tickets 只有 8 張 CLOSED。VIP 名單若與它零交集，two_wide 題問
    # 「結案方式跟滿意度」就會全部是 NULL —— 非空不夠，要問得出東西（§7.4）。
    closed_ids = [t for t in tick if idx["support_ticket_profiles"][t]["closed_at"]]
    vip = [t for t in tick if idx["support_ticket_profiles"][t]["is_vip_flagged"]]
    if len(set(vip) & set(closed_ids)) < 3:
        for t in [t for t in closed_ids if t not in vip][:3]:
            r = idx["support_ticket_profiles"][t]
            r["is_vip_flagged"] = 1
            r["customer_tier_at_intake"] = _pick(["金卡會員", "白金會員"])
            vip.append(t)
    for t in vip:
        r = idx["support_ticket_profiles"][t]
        if r["closed_at"] and not r["survey_responded"]:
            r["survey_responded"] = 1
        if r["survey_responded"]:
            r["satisfaction_label"] = _pick(
                ["VERY_SATISFIED", "SATISFIED", "SATISFIED", "NEUTRAL",
                 "DISSATISFIED"])
    arts = [r[0] for r in q("SELECT id FROM faq_articles ORDER BY id")]
    for i, tid in enumerate(tick):
        if i % 3 == 0 and arts:
            idx["support_ticket_profiles"][tid]["knowledge_article_id"] = \
                arts[i % len(arts)]
    _n = lambda pred: sum(1 for t in tick if pred(idx["support_ticket_profiles"][t]))
    out.append(f"工單：主管介入 {_n(lambda r: r['is_supervisor_involved'])}、"
               f"重開 {_n(lambda r: r['reopened_count'] > 0)}、"
               f"優先處理 {_n(lambda r: r['is_vip_flagged'])}"
               f"（其中已結案 {len(set(vip) & set(closed_ids))} 張）"
               f"，共 {len(tick)} 張，75% 上限是 {int(CEIL * len(tick))}")

    # ---- subscription_profiles（17 列，全部指定，且與母表狀態一致）----
    # subscriptions.status = ACTIVE 6 / CANCELLED 6 / PAUSED 5。
    # PAUSED 是獨立狀態，本來就該對應 is_paused —— 一開始把非 ACTIVE 全當成
    # 「已取消」，等於自己另外發明一組暫停名單，準則 4b 就檢查不到東西了。
    subs = q("SELECT id, status FROM subscriptions ORDER BY id")
    live = [s for s, st in subs if st == "ACTIVE"]
    paused = [s for s, st in subs if st == "PAUSED"]
    dead = [s for s, st in subs if st == "CANCELLED"]
    for sid in paused:
        r = idx["subscription_profiles"][sid]
        r["is_paused"] = 1
        r["paused_at"] = _after(r["created_at"], 43200, 100000)
        r["resume_scheduled_at"] = TODAY + timedelta(days=R.randint(5, 40))
        r["pause_count"] = R.randint(1, 2)
        r["pause_reason"] = _pick(["出國一段時間", "暫時用不到", "經濟考量"])
    for i, sid in enumerate(live + paused):
        if i < 4:                          # 扣款失敗過（跨 ACTIVE 與 PAUSED）
            r = idx["subscription_profiles"][sid]
            r["failed_billing_count"] = R.randint(1, 3)
            r["last_failure_reason"] = _pick(["卡片已過期", "餘額不足",
                                              "發卡行拒絕授權"])
    for i, sid in enumerate(sorted(s for s, _ in subs)):
        r = idx["subscription_profiles"][sid]
        if i < 6:                          # 有試用期
            r["had_trial"] = 1
            r["trial_days"] = _pick([7, 14, 30])
            r["trial_started_at"] = r["created_at"].date()
            r["trial_ended_at"] = (r["created_at"] +
                                   timedelta(days=r["trial_days"])).date()
            if i < 4:                      # 其中 4 個轉付費（真子集）
                r["converted_from_trial"] = 1
                r["conversion_source"] = ["EMAIL", "APP", "AGENT", "SELF"][i]
    for sid in dead:                       # CANCELLED → 一定有取消紀錄（準則 4b）
        r = idx["subscription_profiles"][sid]
        r["is_auto_renew"] = 0
        r["auto_renew_changed_at"] = _after(r["created_at"], 43200, 150000)
        r["cancel_requested_at"] = r["auto_renew_changed_at"]
        r["cancel_reason"] = _pick(["價格考量", "用不到了", "改用其他服務",
                                    "服務品質不如預期"])
        r["cancel_feedback"] = "希望未來能有更彈性的方案選擇"
        r["is_win_back_targeted"] = 1 if R.random() < 0.5 else 0
    _s = lambda pred: sum(1 for r in data["subscription_profiles"] if pred(r))
    out.append(f"訂閱：暫停 {_s(lambda r: r['is_paused'])}（=母表 PAUSED）、"
               f"扣款失敗 {_s(lambda r: r['failed_billing_count'] > 0)}、"
               f"試用 {_s(lambda r: r['had_trial'])}"
               f"（轉付費 {_s(lambda r: r['converted_from_trial'])} 是真子集）、"
               f"已取消 {_s(lambda r: r['cancel_requested_at'])}（=母表 CANCELLED）")

    # ---- promotion_profiles（8 列，最小的表，全部指定）----
    promos = q("SELECT id, is_active FROM promotions ORDER BY id")
    ids = [p for p, _ in promos]
    used = {r[0] for r in q(
        "SELECT DISTINCT promotion_id FROM order_promotions")}
    for i, pid in enumerate(ids):
        r = idx["promotion_profiles"][pid]
        r["is_stackable"] = 0 if i < 3 else 1
        if i < 3:
            r["conflicts_with"] = "全站折扣檔"
            r["excludes_coupon"] = 1 if i < 2 else 0
            r["excludes_gift_card"] = 1 if i == 0 else 0
        r["requires_code"] = 1 if 2 <= i < 5 else 0
        r["is_auto_applied"] = 0 if r["requires_code"] else 1
        if i < 2:                          # 預算用罄
            r["is_budget_exhausted"] = 1
            r["budget_used"] = r["budget_amount"]
            r["exhausted_at"] = _after(r["created_at"], 1440, 20160)
        else:
            r["budget_used"] = round(float(r["budget_amount"]) *
                                     R.uniform(0.1, 0.8), 2)
        r["first_order_only"] = 1 if i in (1, 4) else 0
    # two_wide 題要的：首頁橫幅 + 自動套用，而且真的有訂單用過
    home = [p for p in ids if p in used and not
            idx["promotion_profiles"][p]["requires_code"]][:2]
    if len(home) < 2:
        home = [p for p in ids if p in used][:2]
        for p in home:
            idx["promotion_profiles"][p]["requires_code"] = 0
            idx["promotion_profiles"][p]["is_auto_applied"] = 1
    for p in ids:
        idx["promotion_profiles"][p]["display_slot"] = (
            "HOME_TOP" if p in home else
            _pick(["CATEGORY", "CART", "CHECKOUT"]))
    for pid, active in promos:
        if not active:
            r = idx["promotion_profiles"][pid]
            r["is_paused"] = 1
            r["paused_at"] = _after(r["created_at"], 1440, 30240)
            r["pause_reason"] = _pick(["成效不如預期", "預算重新調配",
                                       "商品供應調整"])
    _p = lambda pred: sum(1 for p in ids if pred(idx["promotion_profiles"][p]))
    out.append(f"檔期（{len(ids)} 檔）："
               f"不可疊加 {_p(lambda r: not r['is_stackable'])}、"
               f"需輸入代碼 {_p(lambda r: r['requires_code'])}、"
               f"預算用罄 {_p(lambda r: r['is_budget_exhausted'])}、"
               f"暫停 {_p(lambda r: r['is_paused'])}（=母表 is_active=0）、"
               f"首頁橫幅+自動套用 {len(home)}（都有訂單用過）")

    # ---- return_profiles（18 列）----
    rets = q("SELECT id, status FROM order_returns ORDER BY id")
    rids = [r for r, _ in rets]
    # 檢驗不符（FAIL）不在這裡指定 —— 它由母表 status=REJECTED 決定，
    # 生成器已經寫進去了。在這裡再指定一次會蓋掉母表，準則 4b 就變成自我應驗。
    for i, rid in enumerate(rids):
        r = idx["return_profiles"][rid]
        if r["inspection_result"] == "FAIL":
            r["is_resellable"] = 0
            r["disposition_decision"] = _pick(["SCRAP", "RETURN_SUPPLIER"])
        if 4 <= i < 7:                     # 物流商責任
            r["is_carrier_fault"] = 1
            r["is_customer_fault"] = 0
            r["is_seller_fault"] = 0
            r["reason_l1"] = "DAMAGED"
            r["reason_l2"] = "運送中碰撞"
            r["fault_note"] = "外箱有明顯擠壓痕跡，經物流商確認為運送疏失"
        if 7 <= i < 11:                    # 換貨
            r["is_exchange"] = 1
            r["exchange_product_id"] = R.randint(1, 40)
            r["exchange_shipped_at"] = _after(r["requested_at"], 10080, 30240)
            r["compensation_type"] = _pick(["COUPON", "POINTS", "GIFT_CARD"])
            r["compensation_note"] = "已補償折價券作為換貨等待的補償"
    # 超過可退期限但仍放行 —— 只能挑已核准的，否則問題問不出東西
    ok = [r for r in rids if idx["return_profiles"][r]["approved_at"]]
    for rid in ok[:2]:
        r = idx["return_profiles"][rid]
        r["is_over_policy_window"] = 1
        r["is_goodwill"] = 1
        r["handling_note"] = "已超過可退期限，經主管同意以專案方式通融受理"
    _r = lambda pred: sum(1 for x in rids if pred(idx["return_profiles"][x]))
    out.append(f"退貨（{len(rids)} 筆）："
               f"檢驗不符 {_r(lambda r: r['inspection_result'] == 'FAIL')}"
               f"（=母表 REJECTED）、"
               f"物流商責任 {_r(lambda r: r['is_carrier_fault'])}、"
               f"換貨 {_r(lambda r: r['is_exchange'])}、"
               f"超期限仍放行 {_r(lambda r: r['is_over_policy_window'])}")
    return out


# ===========================================================================
# 準則檢查
# ===========================================================================
def assert_criteria(conn, plan) -> list[str]:
    """任何一條不成立就 raise —— 資料不對，寫 GT 是浪費時間（§5.1）。"""
    ok = []

    def one(sql):
        return conn.execute(text(sql)).scalar()

    tables = plan["tables"]

    # 準則 1：1:1 —— 列數必須等於母表
    for t, spec in tables.items():
        n, m = one(f"SELECT COUNT(*) FROM {t}"), one(
            f"SELECT COUNT(*) FROM {spec['attach']['to']}")
        if n != m:
            raise RuntimeError(f"{t} 有 {n} 列，母表 {spec['attach']['to']} 有 {m} 列")
    ok.append(f"1:1 對齊：{len(tables)} 張表的列數都等於母表")

    # 準則 2：每一題的條件非空，且涵蓋率不超過 75%
    probes = [
        ("shipment_profiles", "exception_code <> 'NONE'"),
        ("shipment_profiles", "recipient_relation = 'NEIGHBOR'"),
        ("shipment_profiles", "is_redirected = 1"),
        ("shipment_profiles", "volumetric_weight_g > actual_weight_g"),
        ("review_profiles", "is_hidden = 1"),
        ("review_profiles", "is_incentivized = 1"),
        ("review_profiles", "days_after_delivery > 30"),
        ("review_profiles", "is_featured = 1"),
        ("payment_profiles", "is_chargeback = 1"),
        ("payment_profiles", "is_foreign_card = 1"),
        ("payment_profiles", "is_installment = 1"),
        ("payment_profiles", "is_reconciled = 0"),
        ("support_ticket_profiles", "is_sla_breached = 1"),
        ("support_ticket_profiles", "is_supervisor_involved = 1"),
        ("support_ticket_profiles", "reopened_count > 0"),
        ("support_ticket_profiles", "is_vip_flagged = 1"),
        ("subscription_profiles", "is_paused = 1"),
        ("subscription_profiles", "converted_from_trial = 1"),
        ("subscription_profiles", "failed_billing_count > 0"),
        ("subscription_profiles", "cancel_requested_at IS NOT NULL"),
        ("promotion_profiles", "is_stackable = 0"),
        ("promotion_profiles", "is_budget_exhausted = 1"),
        ("promotion_profiles", "requires_code = 1"),
        ("promotion_profiles", "display_slot = 'HOME_TOP' AND is_auto_applied = 1"),
        ("return_profiles", "inspection_result = 'FAIL'"),
        ("return_profiles", "is_carrier_fault = 1"),
        ("return_profiles", "is_over_policy_window = 1 AND approved_at IS NOT NULL"),
        ("return_profiles", "is_exchange = 1"),
    ]
    for t, cond in probes:
        n, tot = one(f"SELECT COUNT(*) FROM {t} WHERE {cond}"), one(
            f"SELECT COUNT(*) FROM {t}")
        if n == 0:
            raise RuntimeError(f"{t}.{cond} 沒有任何一列符合 —— 題目會查無資料")
        if n > CEIL * tot:
            raise RuntimeError(
                f"{t}.{cond} 命中 {n}/{tot} 超過 {CEIL:.0%} —— 沒有鑑別力")
    ok.append(f"鑑別力：{len(probes)} 個題目條件都非空且涵蓋率 <= 75%")

    # 準則 3：two_wide 的交集必須比兩邊各自小
    pairs = [
        ("易碎商品 x 貼標籤",
         """SELECT COUNT(DISTINCT s.id) FROM shipments s
            JOIN order_items oi ON oi.order_id = s.order_id
            JOIN product_profiles pp ON pp.product_id = oi.product_id
            WHERE pp.is_fragile = 1""",
         "SELECT COUNT(*) FROM shipment_profiles WHERE has_fragile_label = 1",
         """SELECT COUNT(DISTINCT s.id) FROM shipments s
            JOIN order_items oi ON oi.order_id = s.order_id
            JOIN product_profiles pp ON pp.product_id = oi.product_id
            JOIN shipment_profiles sp ON sp.shipment_id = s.id
            WHERE pp.is_fragile = 1 AND sp.has_fragile_label = 1"""),
        ("手機下單 x 3D 驗證",
         """SELECT COUNT(*) FROM payments p JOIN order_profiles op
            ON op.order_id = p.order_id WHERE op.device_type = 'MOBILE'""",
         "SELECT COUNT(*) FROM payment_profiles WHERE is_3ds_verified = 1",
         """SELECT COUNT(*) FROM payments p
            JOIN order_profiles op ON op.order_id = p.order_id
            JOIN payment_profiles pp ON pp.payment_id = p.id
            WHERE op.device_type = 'MOBILE' AND pp.is_3ds_verified = 1"""),
    ]
    for name, a_sql, b_sql, i_sql in pairs:
        a, b, i = one(a_sql), one(b_sql), one(i_sql)
        if i == 0 or i >= a or i >= b:
            raise RuntimeError(
                f"{name}：交集 {i} 沒有比兩邊各自小（{a} / {b}）")
        ok.append(f"{name}：{a} / {b}，交集 {i}（真子集）")

    # 準則 4b：與母表狀態一致
    # 三態各自對得起來：CANCELLED<->有取消紀錄、PAUSED<->is_paused、ACTIVE<->兩者皆無
    bad = one("""SELECT COUNT(*) FROM subscription_profiles sp
                 JOIN subscriptions s ON s.id = sp.subscription_id
                 WHERE (s.status = 'CANCELLED') <> (sp.cancel_requested_at IS NOT NULL)
                    OR (s.status = 'PAUSED')    <> (sp.is_paused = 1)""")
    if bad:
        raise RuntimeError(f"{bad} 筆訂閱的狀態與母表 status 三態對不起來")
    bad = one("""SELECT COUNT(*) FROM promotion_profiles pp
                 JOIN promotions p ON p.id = pp.promotion_id
                 WHERE p.is_active = 1 AND pp.is_paused = 1""")
    if bad:
        raise RuntimeError(f"{bad} 檔活動母表標為 active 卻在 profile 裡是暫停")
    bad = one("""SELECT COUNT(*) FROM return_profiles rp
                 JOIN order_returns o ON o.id = rp.return_id
                 WHERE (o.status = 'REJECTED') <> (rp.inspection_result = 'FAIL')""")
    if bad:
        raise RuntimeError(f"{bad} 筆退貨的檢驗結果與母表 status 對不起來")
    bad = one("""SELECT COUNT(*) FROM payment_profiles pp
                 JOIN payments p ON p.id = pp.payment_id
                 WHERE pp.is_chargeback = 1 AND p.status <> 'SUCCESS'""")
    if bad:
        raise RuntimeError(f"{bad} 筆退刷掛在非 SUCCESS 的付款上")
    ok.append("母表狀態一致：訂閱取消、檔期暫停、退刷都與母表對得起來")

    # 準則 5：誘餌成立 —— is_hidden 與 reviews.is_deleted 必須是不同集合
    both = one("""SELECT COUNT(*) FROM review_profiles rp
                  JOIN reviews r ON r.id = rp.review_id
                  WHERE rp.is_hidden = 1 AND r.is_deleted = 1""")
    h = one("SELECT COUNT(*) FROM review_profiles WHERE is_hidden = 1")
    d = one("SELECT COUNT(*) FROM reviews WHERE is_deleted = 1")
    if both != 0:
        raise RuntimeError(
            f"is_hidden 與 is_deleted 有 {both} 列重疊 —— 誘餌題分不出模型用了哪一個")
    ok.append(f"誘餌成立：隱藏 {h} 則 vs 已刪除 {d} 則，零重疊 —— "
              f"模型選錯欄位一定會答錯")

    # 準則 6：沒有自相矛盾的狀態
    contradictions = [
        ("subscription_profiles", "is_paused = 1 AND paused_at IS NULL"),
        ("subscription_profiles", "converted_from_trial = 1 AND had_trial = 0"),
        ("promotion_profiles", "is_budget_exhausted = 1 AND exhausted_at IS NULL"),
        ("promotion_profiles", "requires_code = 1 AND is_auto_applied = 1"),
        ("payment_profiles", "is_chargeback = 1 AND chargeback_at IS NULL"),
        ("payment_profiles", "is_installment = 0 AND installment_periods > 0"),
        ("shipment_profiles", "is_redirected = 1 AND redirect_reason IS NULL"),
        ("review_profiles", "is_hidden = 1 AND hidden_reason IS NULL"),
        ("return_profiles", "is_exchange = 1 AND exchange_product_id IS NULL"),
        ("support_ticket_profiles",
         "is_supervisor_involved = 1 AND escalation_level = 0"),
    ]
    for t, cond in contradictions:
        n = one(f"SELECT COUNT(*) FROM {t} WHERE {cond}")
        if n:
            raise RuntimeError(f"{t} 有 {n} 列自相矛盾：{cond}")
    ok.append(f"一致性：{len(contradictions)} 條矛盾檢查全部為 0 列")
    return ok


# ===========================================================================
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="實際建表並灌資料")
    ap.add_argument("--drop", action="store_true", help="還原：DROP 這七張表")
    args = ap.parse_args()
    log.remove()

    plan = yaml.safe_load(io.open(PLAN_PATH, encoding="utf-8"))
    tables = plan["tables"]
    engine = get_db_manager(MYSQL_URI).engine

    with engine.connect() as conn:
        live = {r[0].lower() for r in conn.execute(text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"))}
        existing = [t for t in tables if t in live]
        baseline = sorted(live - set(tables))

        if args.drop:
            if not existing:
                print("這七張表都不存在，沒有東西要還原。")
                return 0
            print(f"即將 DROP: {existing}")
            before = fingerprints(conn, baseline)
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for t in existing:
                conn.execute(text(f"DROP TABLE IF EXISTS `{t}`"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
            conn.commit()
            after = fingerprints(conn, baseline)
            drift = [t for t in baseline if before[t] != after[t]]
            print(f"已還原。既有 {len(baseline)} 張表指紋"
                  f"{'一致 ✅' if not drift else f'有變動 ❌ {drift}'}")
            return 1 if drift else 0

        print(f"宣告 {len(tables)} 張表 / "
              f"{sum(len(s['columns']) for s in tables.values())} 欄")
        if existing:
            n = {t: conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
                 for t in existing}
            print(f"已存在: {n}")
            if any(n.values()):
                print("已經有資料，不重複寫入。要重建請先 --drop。")
                return 0

        if not args.apply:
            for name, spec in tables.items():
                print(f"\n--- {name}（{len(spec['columns'])} 欄，掛在 "
                      f"{spec['attach']['to']}）")
                print(build_ddl(name, spec)[:200] + " …")
            print("\n（乾跑，未寫入。加 --apply 實際執行）")
            return 0

        print(f"\n取既有 {len(baseline)} 張表的內容指紋…")
        before = fingerprints(conn, baseline)

        for name, spec in tables.items():
            conn.execute(text(build_ddl(name, spec)))
        conn.commit()
        print(f"已建立 {len(tables)} 張表")

        data: dict[str, list[dict]] = {}
        for name, (parent, gen) in GENERATORS.items():
            extra = PARENT_EXTRA[name]
            sel = f"SELECT id, {extra} FROM {parent} ORDER BY id"
            rows = list(conn.execute(text(sel)))
            declared = {c["name"] for c in tables[name]["columns"]} - {"id"}
            payload = []
            for r in rows:
                row = gen(r[0], *tuple(r)[1:])
                if set(row) != declared:
                    raise RuntimeError(
                        f"{name} 生成的欄位與宣告不符 —— 少 "
                        f"{sorted(declared - set(row))}、多 "
                        f"{sorted(set(row) - declared)}。宣告檔是唯一來源（§8 ①）")
                payload.append(row)
            data[name] = payload

        print("\n保證式配置（小表不能靠機率）：")
        for line in guarantee(conn, data):
            print(f"  · {line}")

        total = 0
        for name, payload in data.items():
            cols = sorted({c["name"] for c in tables[name]["columns"]} - {"id"})
            stmt = (f"INSERT INTO {name} ({', '.join(cols)}) "
                    f"VALUES ({', '.join(':' + c for c in cols)})")
            conn.execute(text(stmt), payload)
            total += len(payload)
            print(f"  {name:<26}{len(payload):>5} 列")
        conn.commit()
        print(f"共寫入 {total} 列")

        after = fingerprints(conn, baseline)
        drift = [t for t in baseline if before[t] != after[t]]
        if drift:
            raise RuntimeError(
                f"既有表的內容改變了: {drift}\n"
                "279 題 GT 的預期答案已經失效，必須把這七張表 DROP 掉重來。")
        print(f"\n既有 {len(baseline)} 張表指紋全部一致 ✅ —— 279 題 GT 沒有被動到")

        print("\n資料合理性檢查：")
        for line in assert_criteria(conn, plan):
            print(f"  ✅ {line}")

        ntab = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()")).scalar()
        ncol = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()")).scalar()
        print(f"\n全庫 {ntab} 張表 / {ncol} 欄")

    print("\n接下來（缺一不可）：")
    print("  python tools/gen_ddl.py --write              # 不跑這步生成端看不到新欄位（§8 ①）")
    print("  python tools/check_defence_gt.py             # 語意禁令第 [3] 條要盯的")
    print("  python tools/check_schema_pipeline.py        # 八項閘門")
    print("  python eval/eval_retrieval.py --k            # 零 LLM，看候選天花板掉多少")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
