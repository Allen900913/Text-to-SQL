"""給 19 張干擾表灌合理資料 —— 依 distractor_seed_plan.yaml 的宣告

為什麼要灌（ARCHITECTURE.md §7.10）：

  零列的干擾表有兩個壞處，一個是評估的漏洞、一個是方法論的盲點。

  ① **必然的假通過。** 對帳規則裡 `expect: empty` 的題目「回 0 列就算對」，
     而任何對零列表的查詢都回 0 列。模型選錯表也會被判對 ——
     這比 §6.1 的 `#62` 更嚴重：`#62` 需要「兩欄資料剛好相等」的巧合，
     這一條不需要任何巧合。

  ② **對整類方法瞎掉。** 熱度先驗、值檢索這些「用資料當訊號」的方法，
     在全空的干擾表上會假性滿分 —— 不是方法有效，是靶子是空的。
     benchmark 無法判斷這類方法值不值得投資。

安全性（已驗證，不是推論）：
  · GT SQL **0 題**參照干擾表（全文比對）→ 灌資料不可能改變任何 GT 結果
  · 只往新表 INSERT，不動舊表任何一列
  · 亂數用**專屬種子**，不碰 init_db_ext 的共用序列
    （這個模式的前例是 tools/add_customer_profiles.py）
  · 灌之前必須先過 tools/check_ambiguity.py —— 值域宣告在前、灌在後

五條設計準則，每一條在下面都有對應的 assert（--verify 會全部重跑）：
  1. 列數比例合理，且不能一律偏小（干擾表一律小 → 列數變成免費的答案洩漏）
  2. 值域與正解表重疊（否則值檢索一秒分辨 → 假性有效）
  3. 參照完整性（FK 已建，MySQL 硬擋；這裡再自己檢一次好給出可讀的錯誤）
  4. 時序一致（子事件不早於父事件）
  5. 查得出「像樣但錯」的答案，而不是靜悄悄的空集合

用法：
    python tools/seed_distractor_data.py            # 預覽計畫，不寫入
    python tools/seed_distractor_data.py --apply    # 灌（可重入：先清空再灌）
    python tools/seed_distractor_data.py --verify   # 只跑五條準則的檢查
    python tools/seed_distractor_data.py --clear    # 清空全部干擾表的資料
"""
import io
import os
import random
import sys
from datetime import date, datetime, timedelta

import yaml
from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

PLAN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "distractor_seed_plan.yaml")

# 專屬種子。與 init_db / init_db_ext 的共用序列完全隔離 ——
# 那個序列動一下，既有 159 題的 GT 全部失效（§5.1）。
RNG = random.Random(20260819)

# 2026-08-20 之後新增的產生器**一律用這個獨立亂數源**（ARCHITECTURE.md §10.11）。
#
# 為什麼不能共用 RNG：下面的生成迴圈是 `for t, spec in plan.items()`，
# 走的是**計畫檔的順序**。在中間插入一張新表，它抽走的亂數會讓後面每一張表的
# 資料全部改變 —— 而 #160-195 那 36 題 GT 的答案就是綁在現有資料上的。
# 這與 init_db_ext.py「新的 random 呼叫只能加在最後面」是同一條約束，
# 差別在這裡連「加在最後」都不夠，因為順序由 YAML 決定，不由程式碼決定。
#
# 用獨立種子就完全免疫：主序列一個數字都不會被抽走，
# 既有 17 張表重灌之後仍然逐列相同。
RNG_LATE = random.Random(20260820)

# 已經產生好的表 → (欄位, 列)。給「父表也是這一波新表」的產生器用：
# 計畫檔的順序保證父表先跑（wishlists 在 wishlist_items 之前），
# 子表就能直接讀到父表實際產生了幾列，不必猜也不必重算。
_BUILT: dict[str, tuple] = {}


# ===========================================================================
# 父表快照：所有生成都以真實的父列為準（準則 3、4）
# ===========================================================================

def snapshot(conn) -> dict:
    def rows(sql):
        return [tuple(r) for r in conn.execute(text(sql)).fetchall()]
    return {
        "orders": rows("SELECT id, customer_id, order_date, status, total_amount "
                       "FROM orders ORDER BY id"),
        "payments": rows("SELECT id, order_id, method_id, paid_at FROM payments ORDER BY id"),
        "shipments": rows("SELECT id, order_id, shipped_at, created_at FROM shipments ORDER BY id"),
        "products": rows("SELECT id, price, stock FROM products ORDER BY id"),
        "customers": rows("SELECT id, created_at FROM customers ORDER BY id"),
        "reviews": rows("SELECT id, created_at FROM reviews ORDER BY id"),
        "carts": rows("SELECT id, created_at FROM carts ORDER BY id"),
        "promotions": rows("SELECT id FROM promotions ORDER BY id"),
        "suppliers": rows("SELECT id FROM suppliers ORDER BY id"),
        "warehouses": rows("SELECT id FROM warehouses ORDER BY id"),
        "order_items": rows("SELECT order_id, product_id, quantity FROM order_items"),
        # 第二波的 return_shipments 要接 order_returns —— 它在第一波就已經有資料，
        # 所以可以直接快照，不是同波新表
        "order_returns": rows("SELECT id, requested_at FROM order_returns ORDER BY id"),
    }


def after(ts, lo_h=1, hi_h=720):
    """在父事件之後的一個時間點（準則 4）。"""
    return ts + timedelta(hours=RNG.randint(lo_h, hi_h))


# ===========================================================================
# 逐表產生器。回傳 (欄位名 tuple, 列 list)
#
# 刻意寫成一張表一個函式而不是用計畫檔驅動的通用引擎：
# 每張表的「合理」長得都不一樣（狀態機、時序、與父表的比例），
# 通用引擎會把這些差異壓成參數，反而讓「為什麼這樣灌」看不見。
# 計畫檔負責**宣告意圖**（值域、列數、留空），這裡負責實作並自我檢查。
# ===========================================================================

def gen_order_status_history(s, plan):
    dom_from = plan["values"]["from_status"]
    chain = ["PENDING", "PAID", "SHIPPED", "COMPLETED"]
    out = []
    for oid, _cid, odate, status, _amt in s["orders"]:
        if status == "CANCELLED":
            steps = [("PENDING", "CANCELLED")]
        else:
            end = chain.index(status) if status in chain else 1
            steps = [(chain[i], chain[i + 1]) for i in range(end)] or [("PENDING", "PENDING")]
        t = odate
        for frm, to in steps:
            t = after(t, 1, 96)
            out.append((oid, frm, to, t, RNG.choice(["system", "staff01", "staff02"])))
    assert {f for _, f, _, _, _ in out} <= set(dom_from)
    return ("order_id", "from_status", "to_status", "changed_at", "changed_by"), out


def gen_order_notes(s, plan):
    texts = ["客戶要求指定時段配送", "已電話確認地址", "客戶詢問發票開立", "包裝需加固",
             "內部：庫存需調撥", "內部：待主管覆核"]
    out = []
    for oid, _c, odate, _st, _a in s["orders"]:
        if RNG.random() < 0.30:
            note = RNG.choice(texts)
            out.append((oid, note, 1 if note.startswith("內部") else 0, after(odate, 1, 240)))
    return ("order_id", "note", "is_internal", "created_at"), out


def gen_order_cancellations(s, plan):
    reasons = plan["values"]["reason"]
    by = plan["values"]["cancelled_by"]
    out = [(oid, RNG.choice(reasons), RNG.choice(by), after(odate, 1, 120))
           for oid, _c, odate, st, _a in s["orders"] if st == "CANCELLED"]
    return ("order_id", "reason", "cancelled_by", "cancelled_at"), out


def gen_order_returns(s, plan):
    dom = plan["values"]["status"]
    # 只有已出貨/已完成的訂單才可能退貨（準則 5：查出來要像樣）
    items = {}
    for oid, pid, qty in s["order_items"]:
        items.setdefault(oid, []).append((pid, qty))
    eligible = [(oid, odate) for oid, _c, odate, st, _a in s["orders"]
                if st in ("SHIPPED", "COMPLETED") and oid in items]
    out = []
    for oid, odate in eligible:
        if RNG.random() >= 0.12:
            continue
        pid, qty = RNG.choice(items[oid])
        out.append((oid, pid, RNG.randint(1, max(1, qty)), RNG.choice(dom),
                    after(odate, 24, 720)))
    return ("order_id", "product_id", "quantity", "status", "requested_at"), out


def gen_payment_attempts(s, plan):
    dom = plan["values"]["result"]
    by_order = {oid: (odate, st) for oid, _c, odate, st, _a in s["orders"]}
    out = []
    for _pid, oid, mid, _paid in s["payments"]:
        odate, _st = by_order[oid]
        # 準則 1：每筆付款至少一次嘗試，部分有重試 → 列數必然多於 payments
        fails = RNG.choices([0, 1, 2], weights=[62, 28, 10])[0]
        t = odate
        for _ in range(fails):
            t = after(t, 1, 12)
            out.append((oid, mid, RNG.choice([d for d in dom if d != "OK"]), t))
        out.append((oid, mid, "OK", after(t, 1, 12)))
    return ("order_id", "method_id", "result", "attempted_at"), out


def gen_invoices(s, plan):
    out = []
    n = 0
    for oid, _c, odate, st, _a in s["orders"]:
        if st not in ("PAID", "SHIPPED", "COMPLETED"):
            continue
        n += 1
        out.append((oid, f"AB-{10000000 + n}", f"/{RNG.randint(100000, 999999)}",
                    after(odate, 1, 72), 1 if RNG.random() < 0.05 else 0))
    return ("order_id", "invoice_no", "carrier", "issued_at", "is_voided"), out


def gen_delivery_attempts(s, plan):
    dom = plan["values"]["result"]
    out = []
    for sid, _oid, shipped, created in s["shipments"]:
        base = shipped or created
        tries = RNG.choices([1, 2, 3], weights=[70, 22, 8])[0]
        t = base
        for i in range(tries):
            t = after(t, 6, 96)
            res = "DELIVERED" if i == tries - 1 else RNG.choice(["ABSENT", "REFUSED"])
            out.append((sid, i + 1, res, t))
    assert {r for _, _, r, _ in out} <= set(dom)
    return ("shipment_id", "attempt_no", "result", "attempted_at"), out


def gen_warehouse_transfers(s, plan):
    lo, hi = plan["values"]["quantity"]
    whs = [w for (w,) in s["warehouses"]]
    prods = [p for p, _pr, _st in s["products"]]
    base = min(o[2] for o in s["orders"])
    out = []
    for _ in range(55):
        a, b = RNG.sample(whs, 2)
        out.append((a, b, RNG.choice(prods), RNG.randint(lo, hi), after(base, 1, 8000)))
    return ("from_warehouse_id", "to_warehouse_id", "product_id", "quantity",
            "transferred_at"), out


def gen_product_price_history(s, plan):
    base = min(o[2] for o in s["orders"])
    out = []
    for pid, price, _stock in s["products"]:
        for _ in range(RNG.randint(1, 3)):
            old = round(float(price) * RNG.uniform(0.85, 1.25), 2)
            out.append((pid, old, float(price), after(base, 1, 8000)))
    return ("product_id", "old_price", "new_price", "changed_at"), out


def gen_product_stock_snapshots(s, plan):
    lo, hi = plan["values"]["stock_qty"]
    base = max(o[2] for o in s["orders"]).date()
    out = []
    for pid, _price, stock in s["products"]:
        for d in range(7):
            day = base - timedelta(days=6 - d)
            # 快照在現值附近漂移 —— 語意誘餌要「像」才有誘餌價值（§8.9）
            q = max(lo, min(hi, int(stock) + RNG.randint(-8, 8)))
            out.append((pid, day, q))
    return ("product_id", "snapshot_date", "stock_qty"), out


def gen_product_images(s, plan):
    out = []
    for pid, _price, _stock in s["products"]:
        for i in range(RNG.randint(2, 4)):
            out.append((pid, f"/img/p{pid}_{i + 1}.jpg", 1 if i == 0 else 0, i + 1))
    return ("product_id", "url", "is_primary", "sort_order"), out


def gen_customer_contacts(s, plan):
    types = plan["values"]["contact_type"]
    out = []
    for cid, created in s["customers"]:
        for _ in range(RNG.randint(1, 3)):
            t = RNG.choice(types)
            val = {"PHONE": f"09{RNG.randint(10000000, 99999999)}",
                   "EMAIL": f"c{cid}_{RNG.randint(1, 99)}@example.com",
                   "LINE": f"line_{cid}{RNG.randint(10, 99)}"}[t]
            out.append((cid, t, val, RNG.choice([0, 1])))
    return ("customer_id", "contact_type", "contact_value", "is_verified"), out


def gen_customer_login_logs(s, plan):
    out = []
    for cid, created in s["customers"]:
        for _ in range(RNG.randint(4, 12)):
            out.append((cid, after(created, 1, 12000),
                        f"{RNG.randint(1,223)}.{RNG.randint(0,255)}."
                        f"{RNG.randint(0,255)}.{RNG.randint(1,254)}",
                        0 if RNG.random() < 0.08 else 1))
    return ("customer_id", "logged_in_at", "ip", "success"), out


def gen_support_tickets(s, plan):
    dom = plan["values"]["status"]
    subjects = ["訂單何時出貨", "想更改收件地址", "商品有瑕疵", "發票開立問題",
                "無法登入", "想取消訂單"]
    by_cust = {}
    for oid, cid, odate, _st, _a in s["orders"]:
        by_cust.setdefault(cid, []).append((oid, odate))
    out = []
    for cid, created in s["customers"]:
        if RNG.random() >= 0.60:
            continue
        orders = by_cust.get(cid) or []
        oid, odate = RNG.choice(orders) if orders and RNG.random() < 0.7 else (None, None)
        base = odate or created
        out.append((cid, oid, RNG.choice(subjects), RNG.choice(dom), after(base, 1, 2000)))
    return ("customer_id", "order_id", "subject", "status", "created_at"), out


def gen_review_replies(s, plan):
    texts = ["感謝您的回饋，我們會持續改進。", "很抱歉造成不便，已請客服與您聯繫。",
             "謝謝支持！", "已將您的意見轉達供應商。"]
    out = [(rid, RNG.choice(texts), after(created, 1, 500))
           for rid, created in s["reviews"] if RNG.random() < 0.30]
    return ("review_id", "reply_text", "replied_at"), out


def gen_promotion_rules(s, plan):
    applies = plan["values"]["applies_to"]
    lo, hi = plan["values"]["min_amount"]
    out = [(pid, RNG.randrange(lo, hi + 1, 500), RNG.choice([0, 1]), RNG.choice(applies))
           for (pid,) in s["promotions"]]
    return ("promotion_id", "min_amount", "stackable", "applies_to"), out


def gen_supplier_contracts(s, plan):
    terms = plan["values"]["payment_terms"]
    base = min(o[2] for o in s["orders"]).date()
    out = []
    for i, (sid,) in enumerate(s["suppliers"], 1):
        start = base - timedelta(days=RNG.randint(30, 400))
        out.append((sid, f"CT-{2024}{i:04d}", start,
                    start + timedelta(days=RNG.choice([365, 730])), RNG.choice(terms)))
    return ("supplier_id", "contract_no", "starts_at", "ends_at", "payment_terms"), out


def gen_product_bundles(s, plan):
    """組合包：6 個商品當組合包，各含 2~4 項不同的其他商品。用 RNG_LATE。"""
    pids = [pid for pid, _price, _stock in s["products"]]
    bundles = RNG_LATE.sample(pids, 6)
    out = []
    for b in bundles:
        pool = [p for p in pids if p != b]
        for item in RNG_LATE.sample(pool, RNG_LATE.randint(2, 4)):
            out.append((b, item, RNG_LATE.randint(1, 3)))
    return ("bundle_product_id", "item_product_id", "quantity"), out


def gen_cart_recovery_emails(s, plan):
    """挽回信：六成購物車寄過，clicked=1 蘊含 opened=1。用 RNG_LATE。"""
    out = []
    for cid, created in s["carts"]:
        if RNG_LATE.random() > 0.6:
            continue
        sent = created + timedelta(hours=RNG_LATE.randint(24, 168))
        opened = 1 if RNG_LATE.random() < 0.45 else 0
        clicked = 1 if (opened and RNG_LATE.random() < 0.4) else 0
        out.append((cid, sent, opened, clicked))
    return ("cart_id", "sent_at", "opened", "clicked"), out


# ===========================================================================
# 2026-08-20 第一波擴表 41 → 60（ARCHITECTURE.md §11.3）
#
# 全部用 RNG_LATE。這一波的父表自己也是新表，snapshot() 抓到的是空的，
# 所以**明確指定 id**而不依賴 AUTO_INCREMENT —— 重灌才會逐列相同。
# ===========================================================================

N_DEPT, N_EMP, N_STORE, N_COUPON, N_GIFTCARD = 6, 24, 8, 12, 15

_DEPT_NAMES = ["營運部", "客服部", "倉儲部", "行銷部", "資訊部", "財會部"]
_TITLES = ["專員", "資深專員", "組長", "經理"]
_SURNAMES = "陳林黃張李王吳劉蔡楊許鄭謝洪郭"
_GIVEN = ["宗翰", "怡君", "建宏", "淑芬", "俊傑", "雅雯", "冠廷", "詩涵",
          "承恩", "佳穎", "柏勳", "郁婷"]
_STORE_CITIES = ["台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市",
                 "新竹市", "嘉義市"]
_TAGS = ["熱銷", "新品", "限量", "環保", "送禮首選", "編輯推薦"]
_QUESTIONS = ["這個尺寸有現貨嗎？", "可以開統編嗎？", "保固多久？",
              "有其他顏色嗎？", "適合送禮嗎？", "會不會很佔空間？"]
_ANSWERS = ["目前有現貨，下單後 1-2 個工作天出貨。", "可以，結帳時填寫即可。",
            "原廠保固一年。", "目前只有這個顏色。", "很適合，附精美包裝。"]


def gen_departments(s, plan):
    return ("id", "name", "founded_at"), [
        (i + 1, _DEPT_NAMES[i], date(2018 + i % 5, 1 + i % 12, 1))
        for i in range(N_DEPT)]


def gen_employees(s, plan):
    out = []
    for i in range(N_EMP):
        name = RNG_LATE.choice(_SURNAMES) + RNG_LATE.choice(_GIVEN)
        out.append((i + 1, i % N_DEPT + 1, name, RNG_LATE.choice(_TITLES),
                    date(2020 + i % 6, 1 + i % 12, 1 + i % 28),
                    0 if RNG_LATE.random() < 0.12 else 1))
    return ("id", "department_id", "name", "title", "hired_at", "is_active"), out


def gen_order_assignments(s, plan):
    out = []
    for oid, _c, odate, _st, _a in s["orders"]:
        if RNG_LATE.random() > 0.70:
            continue
        for _ in range(RNG_LATE.choices([1, 2], weights=[85, 15])[0]):
            out.append((oid, RNG_LATE.randint(1, N_EMP),
                        odate + timedelta(hours=RNG_LATE.randint(1, 96))))
    return ("order_id", "employee_id", "assigned_at"), out


def gen_stores(s, plan):
    return ("id", "name", "city", "opened_at", "is_active"), [
        (i + 1, f"{_STORE_CITIES[i]}門市", _STORE_CITIES[i],
         date(2019 + i % 6, 1 + i % 12, 1 + i % 28),
         0 if i == N_STORE - 1 else 1)
        for i in range(N_STORE)]


def gen_store_pickups(s, plan):
    out = []
    for oid, _c, odate, st, _a in s["orders"]:
        if st == "CANCELLED" or RNG_LATE.random() > 0.25:
            continue
        # 一成選了到店取貨但還沒去取 → picked_up_at 為 NULL，測 NULL 語意
        taken = None if RNG_LATE.random() < 0.10 else \
            odate + timedelta(hours=RNG_LATE.randint(24, 480))
        out.append((oid, RNG_LATE.randint(1, N_STORE), taken))
    return ("order_id", "store_id", "picked_up_at"), out


def gen_coupons(s, plan):
    out = []
    for i in range(N_COUPON):
        amt = RNG_LATE.choice([50, 100, 150, 200, 300, 500])
        start = date(2026, 1 + i % 8, 1)
        out.append((i + 1, f"SAVE{amt}{chr(65 + i)}", amt, start,
                    date(2026, 1 + (i % 8) + 3, 28)))
    return ("id", "code", "discount_amount", "valid_from", "valid_to"), out


def gen_coupon_redemptions(s, plan):
    out = []
    for oid, cid, odate, st, _a in s["orders"]:
        if st == "CANCELLED" or RNG_LATE.random() > 0.30:
            continue
        out.append((RNG_LATE.randint(1, N_COUPON), cid, oid,
                    odate + timedelta(minutes=RNG_LATE.randint(1, 120))))
    return ("coupon_id", "customer_id", "order_id", "redeemed_at"), out


def gen_gift_cards(s, plan):
    cust = [cid for cid, _cr in s["customers"]]
    out = []
    for i in range(N_GIFTCARD):
        face = RNG_LATE.choice([500, 1000, 2000, 3000])
        used = RNG_LATE.choice([0, 0, face * 0.2, face * 0.5, face])
        owner = None if RNG_LATE.random() < 0.20 else RNG_LATE.choice(cust)
        out.append((i + 1, f"GC-{20260000 + i}", owner, face,
                    round(face - used, 2), date(2026, 1 + i % 8, 1 + i % 28)))
    return ("id", "card_no", "customer_id", "face_value", "balance",
            "issued_at"), out


def gen_gift_card_transactions(s, plan):
    orders = [(oid, odate) for oid, _c, odate, st, _a in s["orders"]
              if st != "CANCELLED"]
    out = []
    for gid in range(1, N_GIFTCARD + 1):
        # 第一筆一定是儲值（正數），之後才可能有消費（負數）
        base = datetime(2026, 1 + (gid - 1) % 8, 1 + (gid - 1) % 28, 10, 0)
        out.append((gid, None, RNG_LATE.choice([500, 1000, 2000, 3000]), base))
        for _ in range(RNG_LATE.randint(0, 3)):
            oid, odate = RNG_LATE.choice(orders)
            out.append((gid, oid, -RNG_LATE.choice([100, 200, 300, 500]),
                        odate + timedelta(minutes=RNG_LATE.randint(1, 60))))
    return ("gift_card_id", "order_id", "amount", "occurred_at"), out


def gen_loyalty_points(s, plan):
    reasons = plan["values"]["reason"]
    out = []
    for oid, cid, odate, st, amt in s["orders"]:
        if st == "CANCELLED":
            continue
        if RNG_LATE.random() < 0.80:
            out.append((cid, oid, int(float(amt) // 100), "PURCHASE",
                        odate + timedelta(hours=1)))
    # 非消費取得／失效的點數：order_id 為空，測 NULL 語意與 reason 值域
    for cid, created in s["customers"]:
        for _ in range(RNG_LATE.randint(0, 2)):
            r = RNG_LATE.choice([x for x in reasons if x != "PURCHASE"])
            pts = RNG_LATE.randint(20, 200) * (-1 if r in ("REDEEM", "EXPIRE") else 1)
            out.append((cid, None, pts, r,
                        created + timedelta(days=RNG_LATE.randint(30, 500))))
    return ("customer_id", "order_id", "points", "reason", "occurred_at"), out


def gen_subscriptions(s, plan):
    plans = plan["values"]["plan_name"]
    stats = plan["values"]["status"]
    out = []
    for cid, created in s["customers"]:
        if RNG_LATE.random() > 0.30:
            continue
        st = RNG_LATE.choice(stats)
        start = (created + timedelta(days=RNG_LATE.randint(1, 400))).date()
        end = None if st == "ACTIVE" else start + timedelta(
            days=RNG_LATE.randint(30, 300))
        out.append((cid, RNG_LATE.choice(plans), st, start, end))
    return ("customer_id", "plan_name", "status", "started_at", "ended_at"), out


def gen_wishlists(s, plan):
    out, wid = [], 0
    for cid, created in s["customers"]:
        if RNG_LATE.random() > 0.60:
            continue
        wid += 1
        out.append((wid, cid, RNG_LATE.choice(["想買清單", "生日禮物", "口袋名單"]),
                    created + timedelta(days=RNG_LATE.randint(1, 400))))
    return ("id", "customer_id", "name", "created_at"), out


def gen_wishlist_items(s, plan):
    pids = [pid for pid, _p, _st in s["products"]]
    n_lists = len(_BUILT["wishlists"][1])
    out = []
    for wid in range(1, n_lists + 1):
        for pid in RNG_LATE.sample(pids, RNG_LATE.randint(1, 5)):
            out.append((wid, pid, datetime(2026, 6, 1) +
                        timedelta(days=RNG_LATE.randint(0, 70))))
    return ("wishlist_id", "product_id", "added_at"), out


def gen_product_qna(s, plan):
    pids = [pid for pid, _p, _st in s["products"]]
    cust = [cid for cid, _cr in s["customers"]]
    out = []
    for i in range(60):
        answered = RNG_LATE.random() < 0.65
        out.append((RNG_LATE.choice(pids), RNG_LATE.choice(cust),
                    RNG_LATE.choice(_QUESTIONS),
                    RNG_LATE.choice(_ANSWERS) if answered else None,
                    RNG_LATE.randint(1, N_EMP) if answered else None,
                    datetime(2026, 5, 1) + timedelta(days=RNG_LATE.randint(0, 100))))
    return ("product_id", "customer_id", "question", "answer", "answered_by",
            "asked_at"), out


def gen_product_tags(s, plan):
    out = []
    for pid, _price, _stock in s["products"]:
        for tag in RNG_LATE.sample(_TAGS, RNG_LATE.randint(0, 3)):
            out.append((pid, tag, datetime(2026, 4, 1) +
                        timedelta(days=RNG_LATE.randint(0, 120))))
    return ("product_id", "tag", "tagged_at"), out


def gen_audit_log(s, plan):
    acts = plan["values"]["action"]
    tbls = ["orders", "customers", "products", "payments", "shipments"]
    out = [(RNG_LATE.choice(tbls), RNG_LATE.randint(1, 200),
            RNG_LATE.choice(acts),
            RNG_LATE.choice(["staff01", "staff02", "system", "admin"]),
            datetime(2026, 1, 1) + timedelta(minutes=RNG_LATE.randint(0, 320000)))
           for _ in range(300)]
    return ("table_name", "row_id", "action", "actor", "occurred_at"), out


def gen_schema_migrations(s, plan):
    out = [(f"2026{i // 2 + 1:02d}{i % 2 * 15 + 1:02d}_{i + 1:03d}",
            datetime(2026, i // 2 + 1, i % 2 * 15 + 1, 3, 0),
            f"{RNG_LATE.getrandbits(128):032x}")
           for i in range(24)]
    return ("version", "applied_at", "checksum"), out


def gen_job_runs(s, plan):
    stats = plan["values"]["status"]
    names = ["nightly_report", "stock_snapshot", "invoice_export",
             "email_digest", "cache_warmup"]
    out = []
    for i in range(120):
        start = datetime(2026, 6, 1) + timedelta(hours=i * 12)
        st = RNG_LATE.choices(stats, weights=[85, 12, 3])[0]
        out.append((RNG_LATE.choice(names), start,
                    None if st == "RUNNING" else
                    start + timedelta(minutes=RNG_LATE.randint(1, 90)), st))
    return ("job_name", "started_at", "finished_at", "status"), out


def gen_user_sessions(s, plan):
    out = []
    for cid, created in s["customers"]:
        for _ in range(RNG_LATE.randint(2, 6)):
            start = created + timedelta(hours=RNG_LATE.randint(1, 12000))
            out.append((f"{RNG_LATE.getrandbits(160):040x}", cid, start,
                        start + timedelta(hours=RNG_LATE.randint(1, 24))))
    return ("session_key", "customer_id", "created_at", "expires_at"), out


# ===========================================================================
# 2026-08-20 第二波擴表 60 → 80（ARCHITECTURE.md §11.5）—— 全部用 RNG_LATE
# ===========================================================================

_COLORS = ["黑", "白", "銀", "藍", "粉"]
_SIZES = ["S", "M", "L", "XL", "單一尺寸"]
_SHIFTS = ["MORNING", "AFTERNOON", "NIGHT"]
_CHANNELS = ["EMAIL", "SOCIAL", "SEARCH", "DISPLAY"]
_FAQ_CATS = ["訂單", "付款", "配送", "退換貨", "會員"]
_FAQ_TITLES = ["如何查詢訂單狀態", "可以更改收件地址嗎", "支援哪些付款方式",
               "多久會出貨", "如何申請退貨", "退款要多久", "如何加入會員",
               "點數怎麼使用", "發票如何開立", "可以到店取貨嗎"]
_KEYWORDS = ["耳機", "咖啡豆", "運動鞋", "洗衣精", "iphone", "行動電源",
             "泡麵", "羽絨外套", "掃地機器人", "生日禮物"]
_SERVICES = ["REPAIR", "CONSULT", "INSTALL"]
_WARRANTY = ["SUBMITTED", "APPROVED", "REJECTED", "DONE"]
_ERR_TYPES = ["TimeoutError", "ValueError", "IntegrityError",
              "ConnectionError", "KeyError"]


def gen_warehouse_staff(s, plan):
    out = []
    for wid, in [(w[0],) for w in s["warehouses"]]:
        for eid in RNG_LATE.sample(range(1, N_EMP + 1), RNG_LATE.randint(2, 4)):
            out.append((wid, eid, date(2024, RNG_LATE.randint(1, 12),
                                       RNG_LATE.randint(1, 28))))
    return ("warehouse_id", "employee_id", "assigned_at"), out


def gen_store_staff(s, plan):
    out = []
    for sid in range(1, N_STORE + 1):
        for eid in RNG_LATE.sample(range(1, N_EMP + 1), RNG_LATE.randint(2, 5)):
            out.append((sid, eid, date(2024, RNG_LATE.randint(1, 12),
                                       RNG_LATE.randint(1, 28))))
    return ("store_id", "employee_id", "assigned_at"), out


def gen_employee_shifts(s, plan):
    out = []
    for eid in range(1, N_EMP + 1):
        for d in range(14):
            st = RNG_LATE.choice(_SHIFTS)
            out.append((eid, date(2026, 8, 1) + timedelta(days=d), st,
                        8.0 if st != "NIGHT" else 10.0))
    return ("employee_id", "shift_date", "shift_type", "hours"), out


def gen_product_variants(s, plan):
    out, vid = [], 0
    for pid, _price, _stock in s["products"]:
        n = RNG_LATE.randint(1, 4)
        for i, (c, z) in enumerate(
                zip(RNG_LATE.sample(_COLORS, n), RNG_LATE.sample(_SIZES, n))):
            vid += 1
            out.append((vid, pid, c, z, 1 if i == 0 else 0))
    return ("id", "product_id", "color", "size", "is_default"), out


def gen_variant_barcodes(s, plan):
    n_var = len(_BUILT["product_variants"][1])
    out = []
    for vid in range(1, n_var + 1):
        for k in range(RNG_LATE.randint(1, 2)):
            out.append((vid, f"471{RNG_LATE.randint(1000000000, 9999999999)}",
                        date(2025 + k, RNG_LATE.randint(1, 12),
                             RNG_LATE.randint(1, 28))))
    return ("variant_id", "barcode", "registered_at"), out


def gen_newsletters(s, plan):
    return ("id", "subject", "sent_at"), [
        (i + 1, f"第 {i + 1} 期電子報：本月精選",
         datetime(2026, 1 + i % 8, 5 + i % 20, 10, 0)) for i in range(12)]


def gen_newsletter_subscriptions(s, plan):
    out = []
    for cid, created in s["customers"]:
        if RNG_LATE.random() > 0.50:
            continue
        sub = created + timedelta(days=RNG_LATE.randint(0, 200))
        # 兩成退訂 → unsubscribed_at 有值，其餘為 NULL（測 NULL 語意）
        uns = sub + timedelta(days=RNG_LATE.randint(30, 300)) \
            if RNG_LATE.random() < 0.20 else None
        out.append((cid, sub, uns))
    return ("customer_id", "subscribed_at", "unsubscribed_at"), out


def gen_campaigns(s, plan):
    out = []
    for i in range(10):
        start = date(2026, 1 + i % 8, 1)
        out.append((i + 1, f"{2026}Q{i % 4 + 1} {_CHANNELS[i % 4]} 投放",
                    _CHANNELS[i % 4], start, start + timedelta(days=30)))
    return ("id", "name", "channel", "started_at", "ended_at"), out


def gen_campaign_clicks(s, plan):
    cust = [cid for cid, _cr in s["customers"]]
    out = []
    for camp in range(1, 11):
        for _ in range(RNG_LATE.randint(30, 60)):
            out.append((camp,
                        None if RNG_LATE.random() < 0.25 else RNG_LATE.choice(cust),
                        datetime(2026, 1 + (camp - 1) % 8, 1) +
                        timedelta(minutes=RNG_LATE.randint(0, 43000))))
    return ("campaign_id", "customer_id", "clicked_at"), out


def gen_search_logs(s, plan):
    out = []
    for cid, created in s["customers"]:
        for _ in range(RNG_LATE.randint(6, 14)):
            kw = RNG_LATE.choice(_KEYWORDS)
            out.append((None if RNG_LATE.random() < 0.15 else cid, kw,
                        RNG_LATE.randint(0, 12),
                        created + timedelta(hours=RNG_LATE.randint(1, 12000))))
    return ("customer_id", "keyword", "result_count", "searched_at"), out


def gen_product_recommendations(s, plan):
    pids = [pid for pid, _p, _st in s["products"]]
    out = []
    for pid in pids:
        for rank, other in enumerate(
                RNG_LATE.sample([p for p in pids if p != pid], 3), start=1):
            out.append((pid, other, rank))
    return ("product_id", "recommended_product_id", "rank_no"), out


def gen_faq_articles(s, plan):
    out = []
    for i in range(20):
        out.append((i + 1, _FAQ_TITLES[i % len(_FAQ_TITLES)] +
                    ("" if i < len(_FAQ_TITLES) else "（進階）"),
                    _FAQ_CATS[i % len(_FAQ_CATS)],
                    date(2025, 1 + i % 12, 1 + i % 28)))
    return ("id", "title", "category", "published_at"), out


def gen_faq_votes(s, plan):
    cust = [cid for cid, _cr in s["customers"]]
    out = []
    for aid in range(1, 21):
        for _ in range(RNG_LATE.randint(4, 12)):
            out.append((aid,
                        None if RNG_LATE.random() < 0.30 else RNG_LATE.choice(cust),
                        1 if RNG_LATE.random() < 0.72 else 0,
                        datetime(2026, 3, 1) +
                        timedelta(minutes=RNG_LATE.randint(0, 200000))))
    return ("article_id", "customer_id", "is_helpful", "voted_at"), out


def gen_return_shipments(s, plan):
    out = []
    for rid, requested in s["order_returns"]:
        if RNG_LATE.random() > 0.80:
            continue
        shipped = requested + timedelta(days=RNG_LATE.randint(1, 5))
        # 兩成還沒被倉庫收到 → received_at 為 NULL
        received = None if RNG_LATE.random() < 0.20 else \
            shipped + timedelta(days=RNG_LATE.randint(1, 6))
        out.append((rid, f"RT{RNG_LATE.randint(10000000, 99999999)}",
                    shipped, received))
    return ("return_id", "tracking_no", "shipped_at", "received_at"), out


def gen_warranty_claims(s, plan):
    items = {}
    for oid, pid, _qty in s["order_items"]:
        items.setdefault(oid, []).append(pid)
    eligible = [(oid, odate) for oid, _c, odate, st, _a in s["orders"]
                if st in ("SHIPPED", "COMPLETED") and oid in items]
    out = []
    for oid, odate in RNG_LATE.sample(eligible, min(30, len(eligible))):
        out.append((oid, RNG_LATE.choice(items[oid]), RNG_LATE.choice(_WARRANTY),
                    odate + timedelta(days=RNG_LATE.randint(10, 300))))
    return ("order_id", "product_id", "status", "claimed_at"), out


def gen_service_appointments(s, plan):
    cust = [cid for cid, _cr in s["customers"]]
    out = []
    for _ in range(45):
        out.append((RNG_LATE.choice(cust), RNG_LATE.randint(1, N_STORE),
                    RNG_LATE.choice(_SERVICES),
                    datetime(2026, 6, 1) + timedelta(
                        hours=RNG_LATE.randint(0, 1800)),
                    1 if RNG_LATE.random() < 0.78 else 0))
    return ("customer_id", "store_id", "service_type", "scheduled_at",
            "attended"), out


def gen_api_request_logs(s, plan):
    paths = ["/api/orders", "/api/products", "/api/customers", "/api/search",
             "/api/cart", "/api/checkout"]
    out = []
    for i in range(800):
        code = RNG_LATE.choices([200, 201, 400, 404, 500],
                                weights=[78, 8, 6, 5, 3])[0]
        out.append((RNG_LATE.choice(paths), code, RNG_LATE.randint(5, 2400),
                    datetime(2026, 7, 1) + timedelta(minutes=i * 3)))
    return ("path", "status_code", "duration_ms", "requested_at"), out


def gen_feature_flags(s, plan):
    keys = ["new_checkout", "dark_mode", "recommend_v2", "fast_search",
            "coupon_stacking", "store_pickup", "gift_wrap", "loyalty_v3",
            "qna_widget", "wishlist_share", "variant_picker", "faq_search",
            "campaign_tracking", "session_v2", "audit_verbose"]
    return ("flag_key", "is_enabled", "updated_at"), [
        (k, 1 if RNG_LATE.random() < 0.6 else 0,
         datetime(2026, 1 + i % 8, 1 + i % 28, 9, 0))
        for i, k in enumerate(keys)]


def gen_cache_entries(s, plan):
    out = []
    for i in range(200):
        out.append((f"cache:{RNG_LATE.choice(['prod', 'cust', 'ord'])}:{i}",
                    datetime(2026, 8, 15) + timedelta(
                        minutes=RNG_LATE.randint(-5000, 5000)),
                    RNG_LATE.randint(0, 900)))
    return ("cache_key", "expires_at", "hit_count"), out


def gen_error_reports(s, plan):
    out = []
    for i in range(60):
        t = RNG_LATE.choice(_ERR_TYPES)
        out.append((t, f"{t}: 處理第 {RNG_LATE.randint(1, 9999)} 筆請求時發生例外",
                    datetime(2026, 6, 1) + timedelta(
                        hours=RNG_LATE.randint(0, 1700))))
    return ("error_type", "message", "occurred_at"), out


GENERATORS = {
    "order_status_history": gen_order_status_history,
    "order_notes": gen_order_notes,
    "order_cancellations": gen_order_cancellations,
    "order_returns": gen_order_returns,
    "payment_attempts": gen_payment_attempts,
    "invoices": gen_invoices,
    "delivery_attempts": gen_delivery_attempts,
    "warehouse_transfers": gen_warehouse_transfers,
    "product_price_history": gen_product_price_history,
    "product_stock_snapshots": gen_product_stock_snapshots,
    "product_images": gen_product_images,
    "customer_contacts": gen_customer_contacts,
    "customer_login_logs": gen_customer_login_logs,
    "support_tickets": gen_support_tickets,
    "review_replies": gen_review_replies,
    "promotion_rules": gen_promotion_rules,
    "supplier_contracts": gen_supplier_contracts,
    # ↓ 2026-08-20 新增，用 RNG_LATE（見該常數的說明）
    "product_bundles": gen_product_bundles,
    "cart_recovery_emails": gen_cart_recovery_emails,
    # ↓ 2026-08-20 第一波擴表 41 → 60（§11.3），全部用 RNG_LATE
    "departments": gen_departments,
    "employees": gen_employees,
    "order_assignments": gen_order_assignments,
    "stores": gen_stores,
    "store_pickups": gen_store_pickups,
    "coupons": gen_coupons,
    "coupon_redemptions": gen_coupon_redemptions,
    "gift_cards": gen_gift_cards,
    "gift_card_transactions": gen_gift_card_transactions,
    "loyalty_points": gen_loyalty_points,
    "subscriptions": gen_subscriptions,
    "wishlists": gen_wishlists,
    "wishlist_items": gen_wishlist_items,
    "product_qna": gen_product_qna,
    "product_tags": gen_product_tags,
    "audit_log": gen_audit_log,
    "schema_migrations": gen_schema_migrations,
    "job_runs": gen_job_runs,
    "user_sessions": gen_user_sessions,
    # ↓ 2026-08-20 第二波擴表 60 → 80（§11.5），全部用 RNG_LATE
    "warehouse_staff": gen_warehouse_staff,
    "store_staff": gen_store_staff,
    "employee_shifts": gen_employee_shifts,
    "product_variants": gen_product_variants,
    "variant_barcodes": gen_variant_barcodes,
    "newsletters": gen_newsletters,
    "newsletter_subscriptions": gen_newsletter_subscriptions,
    "campaigns": gen_campaigns,
    "campaign_clicks": gen_campaign_clicks,
    "search_logs": gen_search_logs,
    "product_recommendations": gen_product_recommendations,
    "faq_articles": gen_faq_articles,
    "faq_votes": gen_faq_votes,
    "return_shipments": gen_return_shipments,
    "warranty_claims": gen_warranty_claims,
    "service_appointments": gen_service_appointments,
    "api_request_logs": gen_api_request_logs,
    "feature_flags": gen_feature_flags,
    "cache_entries": gen_cache_entries,
    "error_reports": gen_error_reports,
}


# ===========================================================================
# 五條準則的檢查
# ===========================================================================

def verify(conn, plan) -> list[str]:
    """回傳違規清單。空清單 = 五條準則全過。"""
    bad: list[str] = []

    def one(sql):
        return conn.execute(text(sql)).scalar()

    # 準則 1：列數不能一律偏小 —— 至少要有干擾表比它的正解對照表大
    n_att = one("SELECT COUNT(*) FROM payment_attempts")
    n_pay = one("SELECT COUNT(*) FROM payments")
    if n_att <= n_pay:
        bad.append(f"準則1 payment_attempts({n_att}) 必須 > payments({n_pay})："
                   f"干擾表一律偏小的話，列數本身就是答案洩漏")
    n_del = one("SELECT COUNT(*) FROM delivery_attempts")
    n_shp = one("SELECT COUNT(*) FROM shipments")
    if n_del <= n_shp:
        bad.append(f"準則1 delivery_attempts({n_del}) 必須 > shipments({n_shp})")

    # 準則 2：值域要與正解表重疊
    hist = {r[0] for r in conn.execute(text(
        "SELECT DISTINCT to_status FROM order_status_history"))}
    ords = {r[0] for r in conn.execute(text("SELECT DISTINCT status FROM orders"))}
    if not ords <= hist:
        bad.append(f"準則2 order_status_history.to_status 應涵蓋 orders.status，"
                   f"缺 {sorted(ords - hist)} —— 值域不重疊的話，"
                   f"值檢索一秒就分辨得出來，量到的有效性是假的")

    # 準則 3：參照完整性（FK 之外再自己檢一次，錯誤訊息才讀得懂）
    orphan = one("SELECT COUNT(*) FROM payment_attempts a "
                 "LEFT JOIN orders o ON o.id = a.order_id WHERE o.id IS NULL")
    if orphan:
        bad.append(f"準則3 payment_attempts 有 {orphan} 列孤兒")

    # 準則 4：時序一致
    early = one("SELECT COUNT(*) FROM order_status_history h "
                "JOIN orders o ON o.id = h.order_id WHERE h.changed_at < o.order_date")
    if early:
        bad.append(f"準則4 order_status_history 有 {early} 列早於訂單建立時間")
    early2 = one("SELECT COUNT(*) FROM customer_login_logs l "
                 "JOIN customers c ON c.id = l.customer_id "
                 "WHERE l.logged_in_at < c.created_at")
    if early2:
        bad.append(f"準則4 customer_login_logs 有 {early2} 列早於註冊時間")

    # 準則 5：該有資料的表都要查得出東西；刻意留空的表必須真的是空的
    for t, spec in plan.items():
        n = one(f"SELECT COUNT(*) FROM {t}")
        if (spec or {}).get("empty"):
            if n:
                bad.append(f"準則5 {t} 宣告 empty: true 卻有 {n} 列")
        elif not n:
            bad.append(f"準則5 {t} 是空的 —— 空表會讓 expect: empty 的題目假通過")
    return bad


# ===========================================================================

def main() -> int:
    apply_ = "--apply" in sys.argv
    # --only t1,t2 —— 只重灌指定的表，其餘一列都不動。
    # 加這個是因為全量 --apply 會 DELETE 全部 17 張表再重灌，而 #160-195
    # 那 36 題 GT 的答案綁在現有資料上：即使重灌後逐列相同（RNG 決定性），
    # 「先刪光再寫回」這個過程也沒有必要冒險。新增表用 --only 就好。
    only = set()
    for a in sys.argv:
        if a.startswith("--only="):
            only = {x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()}
    clear = "--clear" in sys.argv
    only_verify = "--verify" in sys.argv

    plan = yaml.safe_load(io.open(PLAN_PATH, encoding="utf-8"))
    missing = sorted(set(plan) - set(GENERATORS)
                     - {t for t, s in plan.items() if (s or {}).get("empty")})
    if missing:
        print(f"計畫裡有表沒有產生器 → {missing}")
        return 1

    db = get_db_manager(MYSQL_URI)

    if only_verify:
        with db.engine.connect() as conn:
            bad = verify(conn, plan)
        print("\n".join(f"  ✗ {b}" for b in bad) if bad else "五條準則全部通過")
        return 1 if bad else 0

    with db.engine.connect() as conn:
        snap = snapshot(conn)

    if clear:
        with db.engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for t in plan:
                conn.execute(text(f"DELETE FROM {t}"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        print(f"已清空 {len(plan)} 張干擾表的資料（表結構保留）")
        return 0

    if only:
        unknown = sorted(only - set(plan))
        if unknown:
            print(f"--only 指到計畫裡沒有的表 → {unknown}")
            return 1

    built: dict[str, tuple] = {}
    for t, spec in plan.items():
        # 注意：即使 --only 也要**照計畫順序全部跑一遍產生器**，不能跳過 ——
        # 跳過會少抽亂數，被指定那張表拿到的就不是它原本該拿到的序列。
        # （新表用 RNG_LATE 已經免疫，但這條對未來共用 RNG 的表仍然必要。）
        if (spec or {}).get("empty"):
            built[t] = ((), [])
            continue
        built[t] = GENERATORS[t](snap, spec)
        _BUILT[t] = built[t]
    if only:
        built = {t: v for t, v in built.items() if t in only}

    print(f"{'表':<26}{'列數':>7}   說明")
    for t, (_cols, rows) in built.items():
        tag = "（刻意留空）" if (plan[t] or {}).get("empty") else ""
        print(f"{t:<26}{len(rows):>7}   {tag}")
    total = sum(len(r) for _c, r in built.values())
    print(f"{'合計':<26}{total:>7}")

    if not apply_:
        print("\n這是預覽。加 --apply 才會寫入；寫入前會先清空干擾表既有資料（可重入）。")
        print("⚠️  灌之前先跑 `python tools/check_ambiguity.py` —— 值域宣告在前、灌在後。")
        return 0

    with db.engine.begin() as conn:
        conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
        for t in (only or plan):
            conn.execute(text(f"DELETE FROM {t}"))
        conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        for t, (cols, rows) in built.items():
            if not rows:
                continue
            ph = ", ".join(f":{c}" for c in cols)
            conn.execute(text(f"INSERT INTO {t} ({', '.join(cols)}) VALUES ({ph})"),
                         [dict(zip(cols, r)) for r in rows])
    print(f"\n已寫入 {total} 列。")

    with db.engine.connect() as conn:
        bad = verify(conn, plan)
    print("\n五條準則檢查：")
    print("\n".join(f"  ✗ {b}" for b in bad) if bad else "  全部通過")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
