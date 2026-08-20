"""規模第一波：加 19 張表，把資料庫從 41 張推到 60 張

與 add_distractor_tables.py 的差別（ARCHITECTURE.md §11.3）：

    那一支加的是**純 schema、零資料、零 GT**的干擾表 —— 當時的目標只是
    「跨過 CANDIDATE_N」，檢索評估只讀 INFORMATION_SCHEMA，所以那樣就夠了。

    後來量到那個做法的代價（§10.1）：19 張永遠不是答案的表，讓檢索器只要
    學會「這些永遠別選」就能拿滿分 —— 現實中沒有這種捷徑。而零資料又讓
    `expect: empty` 的題目「選錯表也判對」（§7.10）。

    所以這一波從一開始就照 §10.2 的規則做：
      · 15 張業務表 —— 有資料、有 GT 題，會真的當正確答案
      ·  4 張基礎設施表 —— 有資料、標 never_answered，永遠不是答案
      · 比例 15:4 ≈ 8:2，對上真實企業的分布

為什麼是 60 不是 100：

    41 → 60 時 CANDIDATE_N = 40 開始砍掉三分之一，第一段漏斗從「幾乎沒作用」
    變成真正的召回天花板。這是第一次能量到它。一步跳到 100 會同時觸發
    候選截斷、目錄長度、KMB 圖爆炸，一件都歸因不了。

連通性是刻意的：

    每一張業務表都有 FK 連回既有骨幹（orders / customers / products），
    沒有孤島 —— 孤島對 KMB 沒有意義，也不像真實 schema。
    employees 透過 order_assignments 接上訂單，stores 透過 store_pickups 接上，
    這樣「哪個員工處理的訂單最多」才有路可走。

刻意迴避的東西（與前一支同一份清單，加完會自動驗）：

    不加「運費 / 利潤 / 進貨成本 / 週轉率 / 在庫量 / 信用評分」相關的欄位 ——
    那會讓 #68 / #70 / #140 / #141 四題防禦題靜默失效（§8.8）。
    特別注意兩個原本想加、被這條擋掉的表：
      store_inventory  → 欄位名會命中 #140 的 inventory / stock 探針
      shipping_rates   → 直接命中 #68 的運費
    兩張都拿掉了，不是忘了加。

用法：
    python tools/add_domain_tables.py            # 預覽
    python tools/add_domain_tables.py --apply    # 建表（不寫入任何資料）
    python tools/add_domain_tables.py --drop     # 全部移除，回到 41 張
"""
import os
import sys

from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

# (表名, 表註解, 欄位 DDL)
#
# 表註解的寫法沿用 §7.13 的教訓：**寫出這張表「不是什麼」**。
# 「即時庫存在 products.stock」那種指路句，實測是唯一真的修好 #104 的東西。
DOMAIN_TABLES = [
    # ---------------- 組織與人員 ----------------
    ("departments", "部門主檔：公司內部的部門名稱與成立時間。與商品分類 categories 無關", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '部門唯一ID',
        name VARCHAR(50) NOT NULL COMMENT '部門名稱',
        founded_at DATE COMMENT '成立日期'"""),

    ("employees", "員工主檔：公司內部人員的姓名、職稱與到職日。這是員工不是客戶，客戶在 customers", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '員工唯一ID',
        department_id INT COMMENT '所屬部門ID',
        name VARCHAR(50) NOT NULL COMMENT '員工姓名',
        title VARCHAR(50) COMMENT '職稱',
        hired_at DATE COMMENT '到職日',
        is_active TINYINT(1) DEFAULT 1 COMMENT '是否在職',
        FOREIGN KEY (department_id) REFERENCES departments(id)"""),

    ("order_assignments", "訂單處理指派：哪位員工負責處理這張訂單。一張訂單可能轉手多次，最後一筆才是現任負責人", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '指派紀錄唯一ID',
        order_id INT NOT NULL COMMENT '被指派的訂單ID',
        employee_id INT NOT NULL COMMENT '負責的員工ID',
        assigned_at DATETIME COMMENT '指派時間',
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (employee_id) REFERENCES employees(id)"""),

    # ---------------- 門市 ----------------
    ("stores", "實體門市主檔：店名、所在城市與開幕日。這是實體店面，不是倉庫（倉庫在 warehouses）", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '門市唯一ID',
        name VARCHAR(50) NOT NULL COMMENT '門市名稱',
        city VARCHAR(50) COMMENT '門市所在城市',
        opened_at DATE COMMENT '開幕日期',
        is_active TINYINT(1) DEFAULT 1 COMMENT '是否仍在營業'"""),

    ("store_pickups", "門市取貨紀錄：訂單選擇到店取貨時，在哪間門市、什麼時候取走。宅配出貨在 shipments", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '取貨紀錄唯一ID',
        order_id INT NOT NULL COMMENT '對應訂單ID',
        store_id INT NOT NULL COMMENT '取貨門市ID',
        picked_up_at DATETIME COMMENT '實際取貨時間，未取貨為空',
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (store_id) REFERENCES stores(id)"""),

    # ---------------- 優惠券與禮物卡 ----------------
    ("coupons", "優惠券主檔：券碼、折抵金額與有效期間。這是可輸入的券碼，與 promotions 檔期活動不同", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '優惠券唯一ID',
        code VARCHAR(30) NOT NULL COMMENT '優惠券代碼',
        discount_amount DECIMAL(10,2) COMMENT '折抵金額',
        valid_from DATE COMMENT '生效日',
        valid_to DATE COMMENT '失效日'"""),

    ("coupon_redemptions", "優惠券使用紀錄：哪位客戶在哪張訂單用了哪張券。券本身在 coupons，檔期折扣在 order_promotions", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '使用紀錄唯一ID',
        coupon_id INT NOT NULL COMMENT '使用的優惠券ID',
        customer_id INT NOT NULL COMMENT '使用的客戶ID',
        order_id INT COMMENT '使用在哪張訂單，可為空',
        redeemed_at DATETIME COMMENT '使用時間',
        FOREIGN KEY (coupon_id) REFERENCES coupons(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (order_id) REFERENCES orders(id)"""),

    ("gift_cards", "禮物卡主檔：卡號、面額與目前餘額。餘額是卡片的儲值餘額，不是客戶的信用額度", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '禮物卡唯一ID',
        card_no VARCHAR(30) NOT NULL COMMENT '禮物卡卡號',
        customer_id INT COMMENT '持卡客戶ID，未指定持有人為空',
        face_value DECIMAL(10,2) COMMENT '面額',
        balance DECIMAL(10,2) COMMENT '目前餘額',
        issued_at DATE COMMENT '發卡日期',
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),

    ("gift_card_transactions", "禮物卡交易明細：每次儲值或扣款一筆。正數是儲值、負數是消費", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '交易唯一ID',
        gift_card_id INT NOT NULL COMMENT '禮物卡ID',
        order_id INT COMMENT '消費對應的訂單ID，儲值時為空',
        amount DECIMAL(10,2) COMMENT '異動金額，正數儲值、負數消費',
        occurred_at DATETIME COMMENT '異動時間',
        FOREIGN KEY (gift_card_id) REFERENCES gift_cards(id),
        FOREIGN KEY (order_id) REFERENCES orders(id)"""),

    # ---------------- 會員點數與訂閱 ----------------
    ("loyalty_points", "會員點數異動：每次獲得或使用點數留一筆。點數不是金額，也不是任何評分", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '點數異動唯一ID',
        customer_id INT NOT NULL COMMENT '客戶ID',
        order_id INT COMMENT '關聯訂單ID，非消費取得時為空',
        points INT COMMENT '異動點數，正數獲得、負數使用',
        reason VARCHAR(30) COMMENT '異動原因 (PURCHASE/REDEEM/EXPIRE/BONUS)',
        occurred_at DATETIME COMMENT '異動時間',
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (order_id) REFERENCES orders(id)"""),

    ("subscriptions", "訂閱方案：客戶訂閱的方案名稱、狀態與起訖日。這是定期訂閱，與單次訂單 orders 不同", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '訂閱唯一ID',
        customer_id INT NOT NULL COMMENT '訂閱的客戶ID',
        plan_name VARCHAR(30) COMMENT '方案名稱',
        status VARCHAR(20) COMMENT '訂閱狀態 (ACTIVE/PAUSED/CANCELLED)',
        started_at DATE COMMENT '訂閱起始日',
        ended_at DATE COMMENT '訂閱結束日，仍在訂閱中為空',
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),

    # ---------------- 願望清單與問答 ----------------
    ("wishlists", "願望清單主檔：客戶建立的收藏清單。加入願望清單不是購買，也不是加入購物車", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '清單唯一ID',
        customer_id INT NOT NULL COMMENT '建立清單的客戶ID',
        name VARCHAR(50) COMMENT '清單名稱',
        created_at DATETIME COMMENT '建立時間',
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),

    ("wishlist_items", "願望清單品項：清單裡收藏了哪些商品。收藏 ≠ 購買，銷售請看 order_items", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '品項唯一ID',
        wishlist_id INT NOT NULL COMMENT '所屬清單ID',
        product_id INT NOT NULL COMMENT '收藏的商品ID',
        added_at DATETIME COMMENT '加入時間',
        FOREIGN KEY (wishlist_id) REFERENCES wishlists(id),
        FOREIGN KEY (product_id) REFERENCES products(id)"""),

    ("product_qna", "商品問答：客戶對商品提出的問題與客服回覆。這是購買前的提問，與商品評價 reviews、客服工單 support_tickets 都不同", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '問答唯一ID',
        product_id INT NOT NULL COMMENT '被提問的商品ID',
        customer_id INT NOT NULL COMMENT '提問的客戶ID',
        question TEXT COMMENT '問題內容',
        answer TEXT COMMENT '回覆內容，尚未回覆為空',
        answered_by INT COMMENT '回覆的員工ID',
        asked_at DATETIME COMMENT '提問時間',
        FOREIGN KEY (product_id) REFERENCES products(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (answered_by) REFERENCES employees(id)"""),

    ("product_tags", "商品標籤：行銷用的自由標籤，例如「熱銷」「新品」。與 products.category 主分類、product_categories 關聯表都不同", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '標籤紀錄唯一ID',
        product_id INT NOT NULL COMMENT '商品ID',
        tag VARCHAR(30) COMMENT '標籤文字',
        tagged_at DATETIME COMMENT '標記時間',
        FOREIGN KEY (product_id) REFERENCES products(id)"""),

    # ---------------- 基礎設施（never_answered）----------------
    ("audit_log", "系統稽核軌跡：誰在什麼時候動了哪一筆資料。系統維運用，不是業務資料", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '稽核唯一ID',
        table_name VARCHAR(64) COMMENT '被異動的表名',
        row_id INT COMMENT '被異動的資料列ID',
        action VARCHAR(10) COMMENT '動作 (INSERT/UPDATE/DELETE)',
        actor VARCHAR(50) COMMENT '執行者帳號',
        occurred_at DATETIME COMMENT '發生時間'"""),

    ("schema_migrations", "資料庫版本遷移紀錄：每次 schema 變更執行過哪一個版本。部署工具寫入，不是業務資料", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '遷移紀錄唯一ID',
        version VARCHAR(30) COMMENT '版本編號',
        applied_at DATETIME COMMENT '套用時間',
        checksum VARCHAR(64) COMMENT '腳本雜湊值'"""),

    ("job_runs", "背景排程執行紀錄：每次批次工作的起訖與結果。維運監控用，不是業務資料", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '執行紀錄唯一ID',
        job_name VARCHAR(50) COMMENT '工作名稱',
        started_at DATETIME COMMENT '開始時間',
        finished_at DATETIME COMMENT '結束時間',
        status VARCHAR(20) COMMENT '執行結果 (SUCCESS/FAILED/RUNNING)'"""),

    ("user_sessions", "登入工作階段：網站 session 的建立與到期。框架自動維護，不是業務資料。客戶登入事件請看 customer_login_logs", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT 'Session 唯一ID',
        session_key VARCHAR(64) COMMENT 'Session 金鑰',
        customer_id INT COMMENT '對應客戶ID，未登入為空',
        created_at DATETIME COMMENT '建立時間',
        expires_at DATETIME COMMENT '到期時間',
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),
]

# 建表順序有相依：departments 要在 employees 之前，其餘照 DOMAIN_TABLES 的順序。
NAMES = [n for n, _b, _c in DOMAIN_TABLES]


def existing(conn) -> set[str]:
    return {r[0] for r in conn.execute(text(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE()"))}


def main() -> int:
    apply_ = "--apply" in sys.argv
    drop = "--drop" in sys.argv
    db = get_db_manager(MYSQL_URI)

    with db.engine.connect() as conn:
        present = existing(conn)

    if drop:
        with db.engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS=0"))
            for name in reversed(NAMES):
                conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS=1"))
        with db.engine.connect() as conn:
            print(f"已移除。現在 {len(existing(conn))} 張表")
        return 0

    todo = [n for n in NAMES if n not in present]
    print(f"現有 {len(present)} 張表；這一波要加 {len(todo)} 張（已存在 {len(NAMES) - len(todo)} 張）")
    for name, brief, _cols in DOMAIN_TABLES:
        tag = "" if name in todo else "  （已存在，跳過）"
        print(f"  {name:<26}{brief[:46]}{tag}")

    if not apply_:
        print("\n這是預覽。加 --apply 才會建表（不寫入任何資料）。")
        return 0

    with db.engine.begin() as conn:
        for name, brief, cols in DOMAIN_TABLES:
            if name in present:
                continue
            conn.execute(text(
                f"CREATE TABLE {name} ({cols.strip()}) "
                f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                f"COMMENT='{brief.replace(chr(39), chr(39) * 2)}'"))

    with db.engine.connect() as conn:
        after = existing(conn)
    print(f"\n已建立。現在 {len(after)} 張表")
    print("\n下一步（順序不能顛倒）:")
    print("  python tools/seed_distractor_data.py --only=<新表> --apply  # 灌資料")
    print("  python tools/check_schema_pipeline.py                       # 七項全綠才算完成")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
