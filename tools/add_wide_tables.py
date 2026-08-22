"""就地新增六張寬表 —— 不動現有 80 張表一個位元。

為什麼不重跑 db/init_db.py：那會 TRUNCATE 重建整個資料庫，257 題 GT 全部失效。
這支只做 CREATE + INSERT。

**DDL 從 tools/wide_table_plan.yaml 生成，不在這裡寫第二份欄位清單。**
理由是 ARCHITECTURE.md §8 ① 那個咬過五次的形狀：同一件事有兩個來源，
改了一邊沒改另一邊，而且不會報錯。宣告檔是唯一來源，
sync_table_comments.py 也讀同一份（YAML_SRCS）。

安全性由四件事保證：
  1. CREATE TABLE IF NOT EXISTS + 已有資料就中止，重複執行沒有副作用
  2. 專屬亂數種子 RNG_WIDE，不碰任何既有序列
     —— seed_distractor_data.py 的教訓：生成迴圈走 YAML 順序，
        「加在最後」不夠，必須是獨立的亂數源
  3. **全 80 張表**的內容指紋前後比對。db/init_db.py 的 verify_base_tables()
     只顧 4 張核心表，而 257 題 GT 今天綁在遠不止 4 張表上
  4. 生成的欄位集合必須與宣告**完全相同**（多一個少一個都中止）

用法：
    python tools/add_wide_tables.py            # 乾跑：只印計畫與檢查
    python tools/add_wide_tables.py --apply    # 實際建表並灌資料
    python tools/add_wide_tables.py --drop     # 還原（只 DROP 這六張）
"""
import argparse
import hashlib
import io
import os
import random
import sys
from datetime import date, datetime, timedelta

import yaml
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN_PATH = os.path.join(ROOT, "tools", "wide_table_plan.yaml")

# 專屬亂數源。日期就是這批寬表的建立日 —— 換一個值會產生完全不同的資料，
# 所以一旦灌過就不要再動它（GT 會綁在這份資料上）。
RNG_WIDE = random.Random(20260821)
R = RNG_WIDE

TODAY = date(2026, 8, 21)


def _norm(t: str) -> str:
    """與 sync_table_comments._norm 同一個正規化，兩邊才比得一致。"""
    return " ".join((t or "").split())


# ===========================================================================
# DDL：從宣告檔生成
# ===========================================================================

def build_ddl(name: str, spec: dict) -> str:
    lines = []
    for col in spec["columns"]:
        cm = _norm(col.get("comment")).replace("'", "''")
        lines.append(f"  {col['name']} {col['type']} COMMENT '{cm}'")
    att = spec["attach"]
    lines.append(f"  FOREIGN KEY ({att['fk']}) REFERENCES {att['to']}(id)")
    body = ",\n".join(lines)
    tc = _norm(spec["table_comment"]).replace("'", "''")
    return f"CREATE TABLE IF NOT EXISTS {name} (\n{body}\n) COMMENT '{tc}';"


# ===========================================================================
# 資料生成
# ===========================================================================
# 每個函式回傳「一列」的 dict。欄位集合會被逐列比對宣告，多一個少一個都中止。

def _pick(seq, weights=None):
    return R.choices(seq, weights=weights, k=1)[0]


def _dt(base: date, lo: int, hi: int) -> datetime:
    d = base + timedelta(days=R.randint(lo, hi))
    return datetime(d.year, d.month, d.day, R.randint(8, 21), R.randint(0, 59))


def gen_product_profile(pid: int, i: int) -> dict:
    # EOL 佔約 20% —— 「已停產商品」那題要有答案，但不能多到失去鑑別力
    stage = _pick(["NEW", "GROWTH", "MATURE", "EOL"], [15, 30, 35, 20])
    listed = _dt(date(2024, 1, 1), 0, 700)
    delisted = _dt(listed.date(), 90, 400) if stage == "EOL" else None
    air = 1 if R.random() < 0.18 else 0          # 禁止空運 約 18%
    hazard = _pick(["FLAMMABLE", "BATTERY", "AEROSOL"]) if air else "NONE"
    # 安全認證：刻意讓約 1/4 已過期 —— 那是 discrim 題要問的
    expiry = TODAY + timedelta(days=R.randint(-500, 900))
    complete = _pick([R.randint(30, 59), R.randint(60, 100)], [25, 75])
    return {
        "product_id": pid,
        "lifecycle_stage": stage,
        "listed_at": listed,
        "delisted_at": delisted,
        "is_published": 0 if stage == "EOL" and R.random() < 0.7 else 1,
        "is_searchable": 1 if R.random() < 0.9 else 0,
        "is_giftable": 1 if R.random() < 0.6 else 0,
        "is_preorder": 1 if stage == "NEW" and R.random() < 0.4 else 0,
        "preorder_ship_date": TODAY + timedelta(days=R.randint(7, 60))
                              if stage == "NEW" and R.random() < 0.4 else None,
        "name_en": f"Item {chr(65 + i % 26)}{i:03d}",
        "name_ja": f"アイテム{i:03d}",
        "short_copy": _pick(["日常首選", "口碑熱銷", "職人推薦", "限量供應", "新手入門"]),
        "long_copy": "本商品採用嚴選材質，通過多項檢驗，適合長時間使用。" * R.randint(1, 3),
        "selling_points": ";".join(R.sample(
            ["輕量", "耐用", "省電", "靜音", "防潑水", "可折疊", "快充"], R.randint(2, 4))),
        "target_audience": _pick(["學生族", "通勤族", "銀髮族", "親子家庭", "專業玩家"]),
        "usage_scenario": _pick(["居家日常", "辦公室", "戶外運動", "旅行外出", "禮贈用途"]),
        "care_instructions": _pick(["請以乾布擦拭，避免浸水", "可水洗，勿用漂白劑",
                                    "避免陽光直射與高溫", "定期清潔濾網"]),
        "seo_title": f"精選商品 {i:03d} | 官方旗艦",
        "seo_description": "官方正品保證，快速到貨，提供完整售後服務與保固。",
        "seo_keywords": ",".join(R.sample(
            ["熱銷", "正品", "免運", "現貨", "推薦", "評價"], 3)),
        "canonical_url": f"https://shop.example.com/p/{pid}",
        "image_count": R.randint(1, 12),
        "has_video": 1 if R.random() < 0.35 else 0,
        "has_size_chart": 1 if R.random() < 0.3 else 0,
        "has_manual_pdf": 1 if R.random() < 0.45 else 0,
        "content_completeness": complete,
        "content_updated_at": _dt(date(2026, 1, 1), 0, 220),
        "content_owner": _pick(["林品瑄", "陳彥廷", "黃雅雯", "吳承翰", "鄭子晴"]),
        "translation_status": _pick(["NONE", "PARTIAL", "DONE"], [30, 35, 35]),
        "package_type": _pick(["BOX", "BAG", "PALLET"], [60, 30, 10]),
        "package_length_cm": round(R.uniform(8, 90), 1),
        "package_width_cm": round(R.uniform(6, 70), 1),
        "package_height_cm": round(R.uniform(3, 60), 1),
        "package_weight_g": R.randint(80, 18000),
        "is_fragile": 1 if R.random() < 0.22 else 0,
        "needs_cold_chain": 1 if R.random() < 0.08 else 0,
        "is_air_restricted": air,
        "is_oversized": 1 if R.random() < 0.15 else 0,
        "hazard_class": hazard,
        "origin_certified": 1 if R.random() < 0.55 else 0,
        "safety_cert_no": f"SC-{R.randint(10000, 99999)}",
        "safety_cert_expiry": expiry,
        "energy_label": _pick(["1", "2", "3", "4", "5"]),
        "recyclable_code": _pick(["PET", "PP", "PE", "PAPER", "ALU"]),
        "is_restricted_age": 1 if R.random() < 0.06 else 0,
        "compliance_note": _pick(["無特殊限制", "需檢附進口報單", "限國內銷售",
                                  "須隨附中文標示"]),
        "compliance_checked_at": TODAY - timedelta(days=R.randint(10, 500)),
        "warranty_policy": _pick(["原廠保固一年", "原廠保固兩年", "保固三個月",
                                  "不提供保固"]),
        "return_policy_days": (rpd := _pick([7, 14, 30, 0], [55, 25, 15, 5])),
        # 不可退換 = 沒有退貨天數。恆為 0 的欄位是退化欄位，
        # 任何條件查它都回全集或空集，等於這一欄不存在（同 §7.4 準則 5）。
        "is_return_exempt": 1 if rpd == 0 else 0,
        "created_at": _dt(date(2026, 1, 1), 0, 200),
    }


def gen_order_profile(oid: int, order_date) -> dict:
    ch = _pick(["WEB", "APP", "PHONE", "STORE"], [45, 35, 8, 12])
    dev = "MOBILE" if ch == "APP" else _pick(["DESKTOP", "MOBILE", "TABLET"], [55, 30, 15])
    fraud = 1 if R.random() < 0.14 else 0
    base = order_date if isinstance(order_date, datetime) else \
        datetime(order_date.year, order_date.month, order_date.day, 12, 0)
    guest = 1 if R.random() < 0.18 else 0
    contacted = R.randint(0, 3) if R.random() < 0.35 else 0
    return {
        "order_id": oid,
        "channel": ch,
        "device_type": dev,
        "os_name": _pick(["iOS", "Android", "Windows", "macOS"]),
        "browser_name": _pick(["Chrome", "Safari", "Edge", "Firefox"]),
        "app_version": f"{R.randint(3, 6)}.{R.randint(0, 9)}.{R.randint(0, 9)}"
                       if ch == "APP" else None,
        "screen_size": _pick(["1920x1080", "1440x900", "390x844", "820x1180"]),
        "ip_country": _pick(["TW", "TW", "TW", "HK", "JP"]),
        "utm_source": _pick(["google", "facebook", "direct", "line", "instagram"]),
        "utm_medium": _pick(["cpc", "organic", "email", "referral"], [35, 35, 15, 15]),
        "utm_campaign": f"CMP-{R.randint(1, 10):02d}",
        "referrer_domain": _pick(["google.com", "facebook.com", "(none)", "line.me"]),
        "landing_page": _pick(["/", "/sale", "/new", "/category/3c", "/promo/summer"]),
        "is_first_order": 1 if R.random() < 0.28 else 0,
        "is_guest_checkout": guest,
        "session_minutes": R.randint(2, 75),
        "pages_viewed": R.randint(1, 40),
        "cart_edit_count": R.randint(0, 8),
        "checkout_seconds": R.randint(35, 900),
        "checkout_attempts": _pick([1, 2, 3], [80, 15, 5]),
        "coupon_tried_count": _pick([0, 1, 2, 3], [55, 25, 13, 7]),
        "is_split_payment": 1 if R.random() < 0.09 else 0,
        "installment_periods": _pick([0, 3, 6, 12], [70, 12, 10, 8]),
        "currency_code": "TWD",
        "exchange_rate": 1.0000,
        "invoice_type": _pick(["PERSONAL", "COMPANY", "DONATE"], [70, 22, 8]),
        "invoice_carrier": f"/{R.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}{R.randint(1000000, 9999999)}",
        "tax_id_used": str(R.randint(10000000, 99999999)) if R.random() < 0.22 else None,
        "delivery_preference": _pick(["HOME", "STORE", "LOCKER"], [60, 28, 12]),
        "preferred_time_slot": _pick(["MORNING", "AFTERNOON", "EVENING", "ANY"],
                                     [18, 25, 22, 35]),
        "delivery_note": _pick([None, "請放管理室", "送達前請簡訊通知", "假日再送",
                                "電鈴壞了請敲門"]),
        "is_gift_wrap": 1 if R.random() < 0.14 else 0,
        "gift_message": "生日快樂！" if R.random() < 0.07 else None,
        "hide_price_on_slip": 1 if R.random() < 0.11 else 0,
        "signature_required": 1 if R.random() < 0.25 else 0,
        "contact_before_delivery": 1 if R.random() < 0.31 else 0,
        "support_contact_count": contacted,
        "first_support_at": base + timedelta(days=R.randint(1, 9)) if contacted else None,
        "has_complaint": 1 if contacted and R.random() < 0.4 else 0,
        "satisfaction_survey_sent": 1 if R.random() < 0.6 else 0,
        "survey_responded_at": base + timedelta(days=R.randint(5, 20))
                               if R.random() < 0.25 else None,
        "fraud_review_required": fraud,
        "fraud_review_result": _pick(["PASS", "HOLD", "REJECT"], [70, 20, 10])
                               if fraud else None,
        "reviewed_by": _pick(["風控-王", "風控-李", "風控-張"]) if fraud else None,
        "reviewed_at": base + timedelta(hours=R.randint(1, 48)) if fraud else None,
        "created_at": base,
    }


def gen_employee_profile(eid: int, hired_at, is_active) -> dict:
    remote = _pick(["ONSITE", "HYBRID", "REMOTE"], [40, 40, 20])
    required = _pick([16, 24, 32])
    done = R.randint(0, required + 8)
    hired = hired_at if isinstance(hired_at, date) else TODAY - timedelta(days=800)
    if isinstance(hired, datetime):
        hired = hired.date()
    # 離職與否**由母表的 is_active 決定**，不能自己擲骰子。
    # 第一版各擲各的，結果生出 2 筆「employees.is_active=1 卻有 resigned_at」
    # 的矛盾資料 —— 而且它通過了所有檢查，因為當時只驗「離職不早於到職」。
    # 一致性要對齊的是**母表的狀態**，不只是自己表內的時序。
    offset = R.randint(200, 900)
    resigned = None if is_active else hired + timedelta(days=offset)
    if resigned and resigned > TODAY:
        resigned = TODAY - timedelta(days=R.randint(10, 120))
    promos = R.randint(0, 3)
    return {
        "employee_id": eid,
        "employment_type": _pick(["FULLTIME", "PARTTIME", "CONTRACT", "INTERN"],
                                 [65, 15, 12, 8]),
        "job_level": f"J{R.randint(1, 8)}",
        "job_family": _pick(["工程", "營運", "業務", "行政"]),
        "reports_to": None,
        "probation_end_date": hired + timedelta(days=90),
        "is_probation_passed": 1 if R.random() < 0.93 else 0,
        "last_promotion_date": hired + timedelta(days=R.randint(180, 700)) if promos else None,
        "promotion_count": promos,
        "resigned_at": resigned,
        "resign_reason": _pick(["生涯規劃", "另有他就", "家庭因素"]) if resigned else None,
        "rehire_eligible": 1 if resigned and R.random() < 0.7 else (0 if resigned else 1),
        "work_city": _pick(["台北市", "新北市", "台中市", "高雄市", "新竹市"]),
        "work_site": _pick(["總部大樓", "南港辦公室", "台中據點", "高雄據點"]),
        "desk_no": f"{R.choice('ABCDE')}-{R.randint(1, 60):02d}",
        "remote_policy": remote,
        "remote_days_per_week": 5 if remote == "REMOTE" else (R.randint(1, 3)
                                                              if remote == "HYBRID" else 0),
        "timezone": "Asia/Taipei",
        "education_level": _pick(["HIGHSCHOOL", "BACHELOR", "MASTER", "PHD"],
                                 [10, 55, 30, 5]),
        "major": _pick(["資訊工程", "企業管理", "會計", "工業設計", "統計"]),
        "graduated_school": _pick(["台大", "成大", "交大", "政大", "中央"]),
        "graduated_year": R.randint(2005, 2023),
        "language_primary": "中文",
        "language_secondary": _pick([None, "英文", "日文", "台語"]),
        "english_level": _pick(["A2", "B1", "B2", "C1", "C2"], [15, 30, 30, 20, 5]),
        "certifications": ";".join(R.sample(
            ["PMP", "AWS SAA", "CPA", "TQC", "ISO 內稽"], R.randint(0, 2))) or None,
        "cert_expiry_nearest": TODAY + timedelta(days=R.randint(-200, 700))
                               if R.random() < 0.6 else None,
        "training_hours_ytd": done,
        "training_required_hours": required,
        "last_training_at": TODAY - timedelta(days=R.randint(5, 300)),
        "onboarding_completed": 1 if R.random() < 0.95 else 0,
        "safety_training_expiry": TODAY + timedelta(days=R.randint(-120, 500)),
        "review_grade_last": _pick(["A", "B", "C", "D"], [22, 45, 25, 8]),
        "review_grade_prev": _pick(["A", "B", "C", "D"], [20, 47, 26, 7]),
        "review_date_last": date(2026, 1, R.randint(5, 28)),
        "annual_leave_quota": _pick([56, 80, 104, 120]),
        "annual_leave_used": R.randint(0, 80),
        "sick_leave_used": R.randint(0, 40),
        "overtime_hours_ytd": R.randint(0, 160),
        "emergency_contact_name": _pick(["王小美", "李大同", "張淑芬", "陳志明"]),
        "emergency_contact_phone": f"09{R.randint(10000000, 99999999)}",
        "emergency_contact_relation": _pick(["配偶", "父母", "手足", "友人"]),
        "has_company_laptop": 1 if R.random() < 0.8 else 0,
        "badge_no": f"BD{R.randint(1000, 9999)}",
        "created_at": _dt(date(2026, 1, 1), 0, 60),
    }


def gen_store_profile(sid: int, i: int) -> dict:
    sun_closed = R.random() < 0.3
    def hrs():
        o = R.choice([10, 11, 12])
        return f"{o:02d}:00-{o + 10:02d}:00"
    repair = 1 if R.random() < 0.55 else 0
    install = 1 if R.random() < 0.5 else 0
    trade = 1 if R.random() < 0.45 else 0
    return {
        "store_id": sid,
        "address_line": f"{_pick(['中山', '信義', '大安', '中正', '西屯'])}路{R.randint(1, 500)}號",
        "district": _pick(["中山區", "信義區", "大安區", "中正區", "西屯區", "前鎮區"]),
        "postal_code": str(R.randint(100, 900)),
        "floor_no": _pick(["1F", "2F", "B1", "3F"]),
        "floor_area_ping": round(R.uniform(25, 180), 1),
        "nearest_mrt": _pick(["市政府站", "台北車站", "忠孝復興站", "中山站", "西門站"]),
        "walk_minutes_from_mrt": R.randint(1, 15),
        "parking_spaces": _pick([0, 5, 12, 30], [40, 25, 20, 15]),
        "has_bike_parking": 1 if R.random() < 0.7 else 0,
        "district_type": _pick(["MALL", "STREET", "STATION", "SUBURB"]),
        "phone": f"0{R.randint(2, 7)}-{R.randint(20000000, 89999999)}",
        "email": f"store{sid:02d}@example.com",
        "mon_open": hrs(), "tue_open": hrs(), "wed_open": hrs(),
        "thu_open": hrs(), "fri_open": hrs(), "sat_open": hrs(),
        "sun_open": "CLOSED" if sun_closed else hrs(),
        "weekly_open_hours": round(60 + R.uniform(-8, 12), 1),
        "is_24h": 0,
        "holiday_policy": _pick(["國定假日照常營業", "國定假日縮短營業", "國定假日公休"]),
        "offers_pickup": 1 if R.random() < 0.85 else 0,
        "offers_return": 1 if R.random() < 0.75 else 0,
        "offers_repair": repair,
        "offers_installation": install,
        "offers_trade_in": trade,
        "offers_gift_wrap": 1 if R.random() < 0.6 else 0,
        "offers_consultation": 1 if R.random() < 0.5 else 0,
        "service_counter_count": R.randint(1, 6),
        "fitting_room_count": R.randint(0, 5),
        "has_wifi": 1 if R.random() < 0.9 else 0,
        "has_restroom": 1 if R.random() < 0.6 else 0,
        "has_nursing_room": 1 if R.random() < 0.35 else 0,
        "wheelchair_accessible": 1 if R.random() < 0.7 else 0,
        "has_elevator": 1 if R.random() < 0.6 else 0,
        "has_braille_signage": 1 if R.random() < 0.3 else 0,
        "pet_friendly": 1 if R.random() < 0.4 else 0,
        "last_renovated_at": TODAY - timedelta(days=R.randint(60, 1500)),
        "renovation_budget_level": _pick(["S", "M", "L"]),
        "last_audit_at": TODAY - timedelta(days=R.randint(20, 400)),
        "audit_result": _pick(["PASS", "CONDITIONAL", "FAIL"], [70, 22, 8]),
        # 刻意讓一部分落在今年年底前 —— discrim 題要問這個
        "fire_cert_expiry": TODAY + timedelta(days=R.randint(30, 600)),
        "lease_end_date": TODAY + timedelta(days=R.randint(120, 1800)),
        "created_at": _dt(date(2026, 1, 1), 0, 60),
    }


def gen_supplier_profile(sid: int, i: int) -> dict:
    suspended = TODAY - timedelta(days=R.randint(10, 300)) if R.random() < 0.3 else None
    return {
        "supplier_id": sid,
        "legal_name": f"第 {i + 1} 供應股份有限公司",
        "tax_id": str(R.randint(10000000, 99999999)),
        "registered_country": _pick(["TW", "TW", "CN", "JP", "VN"]),
        "registered_address": f"{_pick(['桃園市', '台中市', '台南市'])}工業區{R.randint(1, 80)}號",
        "established_year": R.randint(1985, 2020),
        "employee_scale": _pick(["1-50", "51-200", "201-1000", "1000+"]),
        "bank_name": _pick(["第一銀行", "國泰世華", "中國信託", "玉山銀行"]),
        "bank_account_last4": f"{R.randint(0, 9999):04d}",
        "payment_terms": _pick(["NET30", "NET60", "PREPAID"], [50, 35, 15]),
        "invoice_method": _pick(["EINVOICE", "PAPER"], [70, 30]),
        "contact_sales_name": _pick(["周經理", "許協理", "蔡專員", "何副理"]),
        "contact_sales_phone": f"0{R.randint(2, 7)}-{R.randint(20000000, 89999999)}",
        "contact_sales_email": f"sales{i}@vendor{i}.example.com",
        "contact_qa_name": _pick(["莊品管", "石品保", "曾工程師"]),
        "contact_qa_email": f"qa{i}@vendor{i}.example.com",
        "contact_logistics_name": _pick(["翁調度", "游倉管", "邱物流"]),
        "contact_logistics_email": f"logi{i}@vendor{i}.example.com",
        "escalation_contact": _pick(["總經理室", "業務副總", "營運長辦公室"]),
        "business_language": _pick(["中文", "中文", "英文", "日文"]),
        "timezone": _pick(["Asia/Taipei", "Asia/Shanghai", "Asia/Tokyo"]),
        "response_sla_hours": _pick([4, 8, 24, 48]),
        "iso9001_certified": 1 if R.random() < 0.7 else 0,
        "iso14001_certified": 1 if R.random() < 0.45 else 0,
        "cert_expiry_nearest": TODAY + timedelta(days=R.randint(-100, 800)),
        "last_audit_at": TODAY - timedelta(days=R.randint(30, 500)),
        "audit_result": _pick(["PASS", "CONDITIONAL", "FAIL"], [60, 28, 12]),
        "audit_findings_open": R.randint(0, 6),
        "quality_grade": _pick(["A", "B", "C"], [35, 45, 20]),
        "defect_rate_ppm": R.randint(50, 4000),
        # 刻意讓一部分低於 90% —— multi_col 題要問這個
        "on_time_delivery_pct": round(R.uniform(72, 99.5), 2),
        "lead_time_committed_days": R.randint(3, 45),
        "min_order_qty": _pick([1, 10, 50, 100, 500]),
        "is_exclusive": 1 if R.random() < 0.25 else 0,
        "is_backup_supplier": 1 if R.random() < 0.35 else 0,
        "accepts_dropship": 1 if R.random() < 0.4 else 0,
        "accepts_returns": 1 if R.random() < 0.75 else 0,
        "nda_signed_at": TODAY - timedelta(days=R.randint(200, 2000)),
        "onboarded_at": TODAY - timedelta(days=R.randint(300, 2500)),
        "suspended_at": suspended,
        "suspend_reason": _pick(["品質異常未改善", "交期嚴重延遲", "合約到期未續約"])
                          if suspended else None,
        "created_at": _dt(date(2026, 1, 1), 0, 60),
    }


def gen_campaign_profile(cid: int, i: int) -> dict:
    budget = R.choice([50000, 80000, 120000, 200000, 300000])
    spend = round(budget * R.uniform(0.35, 1.02), 2)
    impressions = R.randint(20000, 900000)
    clicks = R.randint(200, max(300, impressions // 25))
    conv = R.randint(5, max(6, clicks // 12))
    paused = 1 if R.random() < 0.3 else 0
    return {
        "campaign_id": cid,
        "budget_total": budget,
        "budget_daily": round(budget / R.randint(14, 60), 2),
        "spend_total": spend,
        "spend_pacing_pct": round(spend / budget * 100, 2),
        "bid_strategy": _pick(["CPC", "CPM", "CPA"]),
        "bid_amount": round(R.uniform(3, 90), 2),
        "currency_code": "TWD",
        "audience_name": _pick(["新客拓展包", "回購喚醒包", "高價值客群", "相似受眾"]),
        "audience_size": R.randint(20000, 800000),
        "target_age_min": _pick([18, 20, 25, 30]),
        "target_age_max": _pick([35, 45, 55, 65]),
        "target_gender": _pick(["ALL", "M", "F"], [60, 20, 20]),
        "target_cities": ",".join(R.sample(
            ["台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市"], R.randint(2, 4))),
        "target_interests": ",".join(R.sample(
            ["3C", "居家", "運動", "旅遊", "美食", "親子"], R.randint(2, 3))),
        "exclude_existing_customers": 1 if R.random() < 0.4 else 0,
        "placement": _pick(["FEED", "STORY", "SEARCH", "DISPLAY"]),
        "device_target": _pick(["ALL", "MOBILE", "DESKTOP"], [55, 35, 10]),
        "creative_type": _pick(["IMAGE", "VIDEO", "CAROUSEL"]),
        "creative_count": R.randint(1, 8),
        "creative_updated_at": _dt(date(2026, 2, 1), 0, 180),
        "headline_text": _pick(["夏季全站優惠開跑", "新品上市限時折扣",
                                "會員專屬回饋", "換季出清最後倒數"]),
        "cta_text": _pick(["立即選購", "了解更多", "領取優惠", "免費試用"]),
        "landing_url": f"https://shop.example.com/promo/{cid}",
        "ab_test_group": _pick(["A", "B", "NONE"], [30, 30, 40]),
        "ab_test_variable": _pick(["標題文案", "主視覺", "CTA 按鈕", "受眾包"]),
        "impressions": impressions,
        "clicks": clicks,
        "ctr_pct": round(clicks / impressions * 100, 3),
        "reach": int(impressions / R.uniform(1.2, 3.0)),
        "frequency": round(R.uniform(1.2, 3.0), 2),
        "video_views": R.randint(1000, 60000),
        "video_completion_pct": round(R.uniform(8, 65), 2),
        "conversions": conv,
        "conversion_rate_pct": round(conv / clicks * 100, 3),
        "add_to_cart_count": R.randint(conv, conv * 6),
        "bounce_rate_pct": round(R.uniform(25, 78), 2),
        "avg_session_seconds": R.randint(20, 400),
        "owner_name": _pick(["行銷-趙", "行銷-錢", "行銷-孫"]),
        "agency_name": _pick([None, "藍海整合行銷", "光速數位"]),
        "approved_by": _pick(["行銷總監", "營運長", "執行長"]),
        "approved_at": _dt(date(2026, 1, 1), 0, 200),
        "is_paused": paused,
        "paused_reason": _pick(["成效不如預期", "預算調整", "素材下架"]) if paused else None,
        "created_at": _dt(date(2026, 1, 1), 0, 200),
    }


GENERATORS = {
    "product_profiles":  ("products",  gen_product_profile),
    "order_profiles":    ("orders",    gen_order_profile),
    "employee_profiles": ("employees", gen_employee_profile),
    "store_profiles":    ("stores",    gen_store_profile),
    "supplier_profiles": ("suppliers", gen_supplier_profile),
    "campaign_profiles": ("campaigns", gen_campaign_profile),
}

# 生成器需要母表的哪一欄當第二個參數（除了 id）
PARENT_EXTRA = {
    "product_profiles": None, "order_profiles": "order_date",
    "employee_profiles": "hired_at, is_active", "store_profiles": None,
    "supplier_profiles": None, "campaign_profiles": None,
}


# ===========================================================================
# 保證式配置
# ===========================================================================

def guarantee(conn, data: dict[str, list[dict]]) -> list[str]:
    """把「題目要問的條件」從**機率**改成**保證**。

    第一次灌資料就被這件事咬了：`offers_repair AND installation AND trade_in`
    三個各約 0.5 的旗標獨立抽，8 家門市的期望值是 1.0 —— 實際抽到 **0**；
    供應商暫停往來 p=0.3 × 8 家期望 2.4，也抽到 **0**。

    **小表不能靠機率。** 維度表本來就只有 8~10 列（§7.4 說那是真實的），
    而題目要有鑑別力就得保證條件非空、非全集。跨表交集更是如此 ——
    「維修門市 × 全遠端員工」要兩張表同時配合，各自隨機幾乎不可能對上。

    這裡動的是**已生成但還沒寫入**的列，所以不影響亂數序列。
    """
    notes = []

    def q(sql):
        return list(conn.execute(text(sql)))

    st = data["store_profiles"]
    for r in st[:3]:
        r["offers_repair"] = r["offers_installation"] = r["offers_trade_in"] = 1
    st[0]["sun_open"] = st[1]["sun_open"] = "CLOSED"
    for r in st[:2]:
        r["fire_cert_expiry"] = date(2026, 11, 15)
    notes.append("門市：3 家三服務齊全、2 家週日公休、2 家消防今年底前到期")

    # 供應商只有 8 家，而「暫停往來」「準時率不達標」在業務上都是例外狀態。
    # 只補不清的話，隨機值會讓 6/8 家都被暫停 —— 條件沒有退化成全集，
    # 但也已經不是例外了，鑑別力剩不到一點（第一版就是 6/8 與 7/8）。
    # 所以這裡是**指定整個集合**：該有的補上，不該有的清掉。
    sp = data["supplier_profiles"]
    for i, r in enumerate(sp):
        if i < 3:
            r["suspended_at"] = TODAY - timedelta(days=60)
            r["suspend_reason"] = "品質異常未改善"
        else:
            r["suspended_at"] = None
            r["suspend_reason"] = None
        if 3 <= i < 6:
            r["on_time_delivery_pct"] = round(R.uniform(72, 89.5), 2)
        elif r["on_time_delivery_pct"] < 90:
            r["on_time_delivery_pct"] = round(R.uniform(90.5, 99.5), 2)
    notes.append("供應商：3 家暫停往來、3 家準時率低於 90%（其餘明確清掉）")

    for r in data["campaign_profiles"][:3]:
        r["is_paused"] = 1
        r["paused_reason"] = "成效不如預期"
    notes.append("行銷活動：3 檔暫停投放")

    # --- 跨表交集 1：維修門市 × 全遠端員工 ---------------------------------
    # 第一版只保證「交集非空」，結果 8 家門市**每一家**都配到全遠端員工：
    # 「維修 ∧ 有全遠端員工」= 6 家 = 全部維修門市，employee_profiles 那一半
    # 對答案毫無影響 —— 模型只查 store_profiles 也會全對，雙寬表題等於白配。
    # **非空不等於有鑑別力**（與 §5.1 平手檢查同一個道理）。
    #
    # 所以這裡不是「多加兩個 REMOTE」，而是**指定整個 REMOTE 集合**：
    #   · 主力給「沒有派駐任何門市」的人 —— 在家上班本來就不排店，語意也對
    #   · 只留少數幾位派駐在維修門市，讓交集是維修門市的**真子集**
    # 只認「還在營業」的門市：有一家 stores.is_active=0，讓它進答案的話，
    # 模型多寫一個 s.is_active = 1 就會與 GT 對不起來 —— 那是題目的歧義，
    # 不是模型的錯。把歧義從資料端消掉，比事後補 alt_sql 乾淨。
    live_stores = {r[0] for r in q("SELECT id FROM stores WHERE is_active = 1")}
    repair_ids = {r["store_id"] for r in st
                  if r["offers_repair"] == 1 and r["store_id"] in live_stores}
    pairs = q("SELECT store_id, employee_id FROM store_staff")
    ep = {r["employee_id"]: r for r in data["employee_profiles"]}
    staff: dict[int, set[int]] = {}
    for sid, eid in pairs:
        if eid in ep:
            staff.setdefault(eid, set()).add(sid)

    anchors: list[int] = []
    covered: set[int] = set()
    for eid in sorted(ep, key=lambda e: (len(staff.get(e, ())) or 99, e)):
        mine = staff.get(eid, set())
        if mine and mine <= repair_ids and len(covered | mine) <= 2:
            anchors.append(eid)
            covered |= mine
        if len(covered) == 2:
            break
    assert len(covered) == 2, f"湊不出剛好 2 家維修門市的全遠端員工（{covered}）"

    # 還要一位派駐在**非**維修門市的全遠端員工，否則反過來也會退化：
    # 「有全遠端員工的門市」若剛好都提供維修，模型漏掉 offers_repair 也全對。
    off = next((e for e in sorted(ep)
                if staff.get(e) and staff[e] <= live_stores
                and not (staff[e] & repair_ids)), None)
    assert off is not None, "沒有任何員工只被派在非維修門市，交集會反向退化"

    homebound = [e for e in sorted(ep) if e not in staff][:4]
    remote_set = set(anchors) | set(homebound) | {off}
    for eid, r in ep.items():
        if eid in remote_set:
            r["remote_policy"] = "REMOTE"
            r["remote_days_per_week"] = 5
        elif r["remote_policy"] == "REMOTE":
            # 沒被選中的就不准是 REMOTE，否則交集又會被隨機值撐大
            r["remote_policy"] = "HYBRID"
            r["remote_days_per_week"] = 3
    notes.append(f"跨表：{len(remote_set)} 位全遠端員工（{len(homebound)} 位不排店、"
                 f"{len(anchors)} 位派駐在 {len(covered)}/{len(repair_ids)} 家維修門市、"
                 f"1 位派駐在非維修門市）")

    # --- 跨表交集 2：已暫停供應商 × 仍在架商品 -----------------------------
    susp_ids = {r["supplier_id"] for r in sp[:3]}
    ps = q("SELECT product_id, supplier_id FROM product_suppliers")
    pp = {r["product_id"]: r for r in data["product_profiles"]}
    cand = [p for p, sup in ps if sup in susp_ids and p in pp]
    assert cand, "product_suppliers 裡沒有商品由這三家暫停往來的供應商供貨"
    for pid in cand[:3]:
        pp[pid]["lifecycle_stage"] = "MATURE"
        pp[pid]["delisted_at"] = None
        pp[pid]["is_published"] = 1
    notes.append(f"跨表：{len(cand[:3])} 項在架商品由暫停往來的供應商供貨")

    # --- 跨表交集 3：APP 下單 × GOLD 客戶 ----------------------------------
    gold = q("SELECT o.id FROM orders o JOIN customer_profiles cp "
             "ON cp.customer_id = o.customer_id WHERE cp.tier = 'GOLD' ORDER BY o.id")
    op = {r["order_id"]: r for r in data["order_profiles"]}
    picked = [r[0] for r in gold if r[0] in op][:5]
    assert picked, "沒有任何訂單屬於 GOLD 客戶"
    for oid in picked:
        op[oid]["channel"] = "APP"
        op[oid]["device_type"] = "MOBILE"
        op[oid]["app_version"] = "5.2.1"
    notes.append(f"跨表：{len(picked)} 張 GOLD 客戶的訂單走 APP 通路")

    return notes


# ===========================================================================
# 指紋
# ===========================================================================

def fingerprints(conn, tables) -> dict[str, str]:
    """每張表的內容指紋。ORDER BY 1 是為了讓結果與列的實體順序無關。"""
    import pandas as pd
    out = {}
    for t in tables:
        df = pd.read_sql(f"SELECT * FROM `{t}`", conn)
        if len(df.columns):
            df = df.sort_values(by=list(df.columns)).reset_index(drop=True)
        out[t] = hashlib.sha256(df.to_csv(index=False).encode()).hexdigest()[:12]
    return out


# ===========================================================================
# 五條準則 + 題目可答性
# ===========================================================================

def assert_criteria(conn, plan) -> list[str]:
    """回傳檢查通過的敘述；任何一條不成立就 raise。

    §7.4 的五條準則，加上「題目答得出來且不是平手」——
    後者是 §5.1 的教訓：平手題的答案是任意的，寫了也不能用。
    """
    ok = []

    def scalar(sql):
        return conn.execute(text(sql)).scalar()

    # 準則 1：列數與母表一致（1:1）
    for t, (parent, _) in GENERATORS.items():
        a, b = scalar(f"SELECT COUNT(*) FROM {t}"), scalar(f"SELECT COUNT(*) FROM {parent}")
        assert a == b, f"{t} 有 {a} 列但 {parent} 有 {b} 列，1:1 不成立"
    ok.append(f"準則1 列數：六張表都與母表 1:1")

    # 準則 3：參照完整性（FK 已建，這裡再確認沒有孤兒）
    for t, (parent, _) in GENERATORS.items():
        fk = plan["tables"][t]["attach"]["fk"]
        orphan = scalar(f"SELECT COUNT(*) FROM {t} x LEFT JOIN {parent} p "
                        f"ON x.{fk} = p.id WHERE p.id IS NULL")
        assert orphan == 0, f"{t} 有 {orphan} 筆孤兒"
    ok.append("準則3 參照完整性：無孤兒")

    # 準則 4：時序一致
    bad = scalar("SELECT COUNT(*) FROM product_profiles "
                 "WHERE delisted_at IS NOT NULL AND delisted_at < listed_at")
    assert bad == 0, f"product_profiles 有 {bad} 筆下架早於上架"
    bad = scalar("SELECT COUNT(*) FROM employee_profiles p JOIN employees e "
                 "ON e.id = p.employee_id WHERE p.resigned_at IS NOT NULL "
                 "AND p.resigned_at < DATE(e.hired_at)")
    assert bad == 0, f"employee_profiles 有 {bad} 筆離職早於到職"
    ok.append("準則4 時序一致：下架不早於上架、離職不早於到職")

    # 準則 4b：與母表的狀態一致（不只是自己表內的時序）
    bad = scalar("SELECT COUNT(*) FROM employee_profiles p JOIN employees e "
                 "ON e.id = p.employee_id "
                 "WHERE (p.resigned_at IS NOT NULL) <> (e.is_active = 0)")
    assert bad == 0, f"有 {bad} 位員工的 resigned_at 與 employees.is_active 矛盾"
    ok.append("準則4b 母表狀態一致：離職日與 employees.is_active 完全對應")

    # 準則 5：每一題都查得出「非空、非全部」的答案
    probes = {
        "已停產商品": "SELECT COUNT(*) FROM product_profiles WHERE lifecycle_stage='EOL'",
        "禁止空運": "SELECT COUNT(*) FROM product_profiles WHERE is_air_restricted=1",
        "安全認證過期": f"SELECT COUNT(*) FROM product_profiles WHERE safety_cert_expiry < '{TODAY}'",
        "內容完整度<60": "SELECT COUNT(*) FROM product_profiles WHERE content_completeness < 60",
        "APP 下單": "SELECT COUNT(*) FROM order_profiles WHERE channel='APP'",
        "需人工覆核": "SELECT COUNT(*) FROM order_profiles WHERE fraud_review_required=1",
        "送達前電聯": "SELECT COUNT(*) FROM order_profiles WHERE contact_before_delivery=1",
        "訪客結帳": "SELECT COUNT(*) FROM order_profiles WHERE is_guest_checkout=1",
        "全遠端員工": "SELECT COUNT(*) FROM employee_profiles WHERE remote_policy='REMOTE'",
        "訓練未達標": "SELECT COUNT(*) FROM employee_profiles "
                      "WHERE training_hours_ytd < training_required_hours",
        "考核 A": "SELECT COUNT(*) FROM employee_profiles WHERE review_grade_last='A'",
        "週日公休": "SELECT COUNT(*) FROM store_profiles WHERE sun_open='CLOSED'",
        "三服務齊全": "SELECT COUNT(*) FROM store_profiles "
                      "WHERE offers_repair=1 AND offers_installation=1 AND offers_trade_in=1",
        "消防快到期": "SELECT COUNT(*) FROM store_profiles "
                      "WHERE fire_cert_expiry <= '2026-12-31'",
        "準時率<90": "SELECT COUNT(*) FROM supplier_profiles WHERE on_time_delivery_pct < 90",
        "已暫停供應商": "SELECT COUNT(*) FROM supplier_profiles WHERE suspended_at IS NOT NULL",
        "已暫停投放": "SELECT COUNT(*) FROM campaign_profiles WHERE is_paused=1",
    }
    totals = {"product_profiles": scalar("SELECT COUNT(*) FROM product_profiles"),
              "order_profiles": scalar("SELECT COUNT(*) FROM order_profiles"),
              "employee_profiles": scalar("SELECT COUNT(*) FROM employee_profiles"),
              "store_profiles": scalar("SELECT COUNT(*) FROM store_profiles"),
              "supplier_profiles": scalar("SELECT COUNT(*) FROM supplier_profiles"),
              "campaign_profiles": scalar("SELECT COUNT(*) FROM campaign_profiles")}
    # 上限 75%：只擋「等於全集」是不夠的。8 家供應商有 7 家準時率不達標時，
    # 條件在形式上還是真子集，實際上已經退化成「幾乎全部」——
    # 模型漏掉那個條件也照樣答對。鑑別力要的是**兩邊都不極端**。
    CEIL = 0.75
    degenerate = []
    for label, sql in probes.items():
        n = scalar(sql)
        tbl = [t for t in totals if t in sql][0]
        if n == 0 or n > CEIL * totals[tbl]:
            degenerate.append(f"{label}={n}/{totals[tbl]}")
    assert not degenerate, ("這些條件空集合或涵蓋超過 75%，題目沒有鑑別力: "
                            + ", ".join(degenerate))
    ok.append(f"準則5 可答性：{len(probes)} 個題目條件都落在 1 ~ {int(CEIL * 100)}% 之間")

    # §5.1 平手檢查：「點擊率最高的那一檔活動」必須有唯一解
    top = scalar("SELECT COUNT(*) FROM campaign_profiles WHERE ctr_pct = "
                 "(SELECT MAX(ctr_pct) FROM campaign_profiles)")
    assert top == 1, f"點擊率最高的活動有 {top} 檔並列 —— 那題的答案是任意的（§5.1）"
    ok.append("§5.1 平手檢查：點擊率最高的活動唯一")

    # --- 雙寬表題：交集要是**真子集**，不只是非空 ---------------------------
    # 第一版只 assert n > 0。結果「維修門市 × 全遠端員工」= 6 = 全部維修門市，
    # 也就是 employee_profiles 那個條件完全不影響答案 —— 模型只查一張表也全對。
    # 交集題的鑑別力在於「兩邊都要對」，所以判準是 0 < 交集 < 兩邊各自的大小。
    pairs = [
        ("暫停供應商 × 在架商品",
         "SELECT COUNT(*) FROM product_profiles pp "
         "JOIN product_suppliers ps ON ps.product_id = pp.product_id "
         "JOIN supplier_profiles sp ON sp.supplier_id = ps.supplier_id "
         "WHERE sp.suspended_at IS NOT NULL AND pp.delisted_at IS NULL",
         "SELECT COUNT(*) FROM product_suppliers ps JOIN supplier_profiles sp "
         "ON sp.supplier_id = ps.supplier_id WHERE sp.suspended_at IS NOT NULL",
         "SELECT COUNT(*) FROM product_suppliers ps JOIN product_profiles pp "
         "ON pp.product_id = ps.product_id WHERE pp.delisted_at IS NULL"),
        ("APP 下單 × GOLD 客戶",
         "SELECT COUNT(*) FROM order_profiles op JOIN orders o ON o.id = op.order_id "
         "JOIN customer_profiles cp ON cp.customer_id = o.customer_id "
         "WHERE op.channel='APP' AND cp.tier='GOLD'",
         "SELECT COUNT(*) FROM order_profiles WHERE channel='APP'",
         "SELECT COUNT(*) FROM orders o JOIN customer_profiles cp "
         "ON cp.customer_id = o.customer_id WHERE cp.tier='GOLD'"),
        ("維修門市 × 全遠端員工",
         "SELECT COUNT(DISTINCT sp.store_id) FROM store_profiles sp "
         "JOIN store_staff ss ON ss.store_id = sp.store_id "
         "JOIN employee_profiles ep ON ep.employee_id = ss.employee_id "
         "WHERE sp.offers_repair=1 AND ep.remote_policy='REMOTE'",
         "SELECT COUNT(*) FROM store_profiles WHERE offers_repair=1",
         "SELECT COUNT(DISTINCT ss.store_id) FROM store_staff ss "
         "JOIN employee_profiles ep ON ep.employee_id = ss.employee_id "
         "WHERE ep.remote_policy='REMOTE'"),
    ]
    for label, q_inter, q_a, q_b in pairs:
        inter, a, b = scalar(q_inter), scalar(q_a), scalar(q_b)
        assert inter > 0, f"「{label}」是空集合"
        assert inter < a and inter < b, (
            f"「{label}」交集 {inter} 沒有比兩邊各自小（{a} / {b}）—— "
            f"其中一邊的條件不影響答案，雙寬表題等於只考一張表")
        ok.append(f"雙寬表交集：{label} = {inter}（真子集，兩邊分別是 {a} / {b}）")

    # 歸因題：最高點擊率的那一檔，訂單數與金額都要能把它與其他檔分開。
    # 訂單數 20 有三檔並列 —— 只問「幾張」的話，選錯活動也有機會蒙對，
    # 所以題目多問一個總金額（§5.1：答案要能鑑別，不是能算出來就好）。
    rows = list(conn.execute(text(
        "SELECT cp.campaign_id, SUM(o.total_amount) amt FROM campaign_profiles cp "
        "JOIN order_profiles op ON op.utm_campaign = CONCAT('CMP-', LPAD(cp.campaign_id, 2, '0')) "
        "JOIN orders o ON o.id = op.order_id GROUP BY cp.campaign_id")))
    amts = [r[1] for r in rows]
    assert len(rows) == scalar("SELECT COUNT(*) FROM campaign_profiles"),         "有活動歸因不到任何訂單 —— CMP-NN 與 campaigns.id 的對應斷了"
    assert len(set(amts)) == len(amts), "有兩檔活動的歸因金額相同，歸因題分不出對錯"
    ok.append(f"歸因題：{len(rows)} 檔活動的歸因金額兩兩相異")

    return ok


# ===========================================================================

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="實際建表並灌資料")
    ap.add_argument("--drop", action="store_true", help="還原：DROP 這六張表")
    args = ap.parse_args()

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
                print("這六張表都不存在，沒有東西要還原。")
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
                print(build_ddl(name, spec)[:220] + " …")
            print("\n（乾跑，未寫入。加 --apply 實際執行）")
            return 0

        # ---- 前指紋 -----------------------------------------------------
        print(f"\n取既有 {len(baseline)} 張表的內容指紋…")
        before = fingerprints(conn, baseline)

        # ---- 建表 -------------------------------------------------------
        for name, spec in tables.items():
            conn.execute(text(build_ddl(name, spec)))
        conn.commit()
        print(f"已建立 {len(tables)} 張表")

        # ---- 生成（先全部產出，還不寫入）---------------------------------
        data: dict[str, list[dict]] = {}
        for name, (parent, gen) in GENERATORS.items():
            extra = PARENT_EXTRA[name]
            sel = f"SELECT id{', ' + extra if extra else ''} FROM {parent} ORDER BY id"
            rows = list(conn.execute(text(sel)))
            declared_cols = {c["name"] for c in tables[name]["columns"]} - {"id"}
            payload = []
            for i, r in enumerate(rows):
                row = gen(r[0], *(tuple(r)[1:] if extra else (i,)))
                if set(row) != declared_cols:
                    miss, extra_c = declared_cols - set(row), set(row) - declared_cols
                    raise RuntimeError(
                        f"{name} 生成的欄位與宣告不符 —— 少 {sorted(miss)}、"
                        f"多 {sorted(extra_c)}。宣告檔是唯一來源（§8 ①）")
                payload.append(row)
            data[name] = payload

        # ---- 保證式配置（在寫入之前）--------------------------------------
        print("\n保證式配置（小表不能靠機率）：")
        for line in guarantee(conn, data):
            print(f"  · {line}")

        # ---- 寫入 ---------------------------------------------------------
        total = 0
        for name, payload in data.items():
            cols = sorted({c["name"] for c in tables[name]["columns"]} - {"id"})
            stmt = (f"INSERT INTO {name} ({', '.join(cols)}) "
                    f"VALUES ({', '.join(':' + c for c in cols)})")
            conn.execute(text(stmt), payload)
            total += len(payload)
            print(f"  {name:<20}{len(payload):>5} 列")
        conn.commit()
        print(f"共寫入 {total} 列")

        # ---- 後指紋 -----------------------------------------------------
        after = fingerprints(conn, baseline)
        drift = [t for t in baseline if before[t] != after[t]]
        if drift:
            raise RuntimeError(
                f"既有表的內容改變了: {drift}\n"
                "257 題 GT 的預期答案已經失效，必須把這六張表 DROP 掉重來。")
        print(f"\n既有 {len(baseline)} 張表指紋全部一致 ✅ —— 257 題 GT 沒有被動到")

        # ---- 五條準則 ---------------------------------------------------
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
    print("  python tools/gen_ddl.py --write          # 不跑這步，生成端看不到新欄位（§8 ①）")
    print("  python tools/check_schema_pipeline.py    # 八項閘門，第 6 項會要求新表配題")
    print("  python eval/eval_retrieval.py --k        # 零 LLM，看候選天花板掉多少")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
