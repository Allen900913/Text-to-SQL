"""
Schema 擴充 — 從 4 張表擴到 20 張
====================================
目的不是「讓資料庫更大」，是製造檢索難度。先導實驗（experiments/
retrieval_pilot.py）顯示所有檢索決策只需要 schema 寬度就能量測，
不需要資料量，所以這一步刻意維持小資料（每張表數百列）。

鐵則：既有 4 張表（customers / products / orders / order_items）
      一列都不動。那 100 題的 ground truth 必須繼續有效，
      init_db.py 會在最後比對它們的 sha256。
      因此本模組的所有 random 呼叫都排在既有資料產生「之後」——
      插進去就會平移亂數序列，既有資料跟著全變。

刻意埋設的難點（這些就是 2a 的產出，不是副作用）：

  1. 同名不同義的 status —— 四種完全不同的值域：
       orders.status     PENDING / PAID / SHIPPED / COMPLETED / CANCELLED
       payments.status   SUCCESS / FAILED / REFUNDED
       shipments.status  PREPARING / IN_TRANSIT / DELIVERED / RETURNED
       refunds.status    REQUESTED / APPROVED / REJECTED / DONE
     不加表前綴的 SQL 在這裡會變成真的有歧義。

  2. 時間陷阱 —— 一連串意義不同的時間欄位：
       orders.order_date → payments.paid_at → shipments.shipped_at
       → shipments.delivered_at，另有 reviews.created_at、carts.updated_at。
     「訂單什麼時候完成的」到底指哪一個，只能靠 COMMENT 分辨。

  3. 金額陷阱 —— orders.total_amount / order_items.unit_price /
     products.price / payments.amount / refunds.amount /
     order_promotions.discount_amount / product_suppliers.supply_price。

  4. 語意捷徑 —— browse_logs 與 cart_items 都在 customers 與 products
     之間製造 2 跳路徑。問「買了什麼」時最短路會走瀏覽紀錄，
     拓撲對、語意錯。這是先導實驗抓到的 bug，現在把它做進真實 schema。

  5. 兩條路通同一個概念 —— products.category 是反正規化的字串，
     product_categories 是正規化的關聯表，兩者並存。

  6. 雜訊欄位 —— is_deleted / sync_version / updated_by / external_ref，
     測試模型會不會自作主張加上 is_deleted = 0。

  7. 一張寬表 —— product_specs 25 欄且大量 NULL，用來測欄位級剪枝。

資料的內部一致性是刻意維護的（付款晚於下單、出貨晚於付款、只能評價
買過的商品）。#83 的教訓：seed 時兩個獨立亂數會產生「首單早於註冊」
這種不可能的資料，讓題目測不出東西。
"""
import random
from datetime import timedelta

from sqlalchemy import text

from langgraph_sql.data_anchor import DATA_ANCHOR_DATETIME

# ===========================================================================
# DDL
# ===========================================================================

EXT_DDL = [
    """
    CREATE TABLE IF NOT EXISTS categories (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '分類唯一ID',
        name VARCHAR(50) NOT NULL COMMENT '分類名稱',
        parent_id INT NULL COMMENT '上層分類ID，NULL 表示最上層',
        sort_order INT NOT NULL DEFAULT 0 COMMENT '同層顯示順序',
        is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否啟用中',
        FOREIGN KEY (parent_id) REFERENCES categories(id)
    ) COMMENT '商品分類表（正規化版本；products.category 是反正規化的字串欄位）';
    """,
    """
    CREATE TABLE IF NOT EXISTS suppliers (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '供應商唯一ID',
        name VARCHAR(100) NOT NULL COMMENT '供應商名稱（非客戶姓名）',
        contact_email VARCHAR(100) COMMENT '供應商聯絡信箱',
        phone VARCHAR(20) COMMENT '供應商電話（非客戶電話）',
        city VARCHAR(50) COMMENT '供應商所在城市（非客戶居住城市）',
        created_at DATETIME NOT NULL COMMENT '建檔時間'
    ) COMMENT '供應商表';
    """,
    """
    CREATE TABLE IF NOT EXISTS warehouses (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '倉庫唯一ID',
        name VARCHAR(50) NOT NULL COMMENT '倉庫名稱',
        city VARCHAR(50) NOT NULL COMMENT '倉庫所在城市',
        capacity INT NOT NULL COMMENT '倉儲容量（單位：箱）',
        is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否營運中'
    ) COMMENT '倉庫表';
    """,
    """
    CREATE TABLE IF NOT EXISTS payment_methods (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '付款方式唯一ID',
        name VARCHAR(50) NOT NULL COMMENT '付款方式名稱，例如信用卡、貨到付款',
        channel VARCHAR(30) NOT NULL COMMENT '金流管道代碼',
        fee_rate DECIMAL(5,4) NOT NULL COMMENT '手續費率（0.0250 表示 2.5%）',
        is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否開放使用'
    ) COMMENT '付款方式表';
    """,
    """
    CREATE TABLE IF NOT EXISTS promotions (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '促銷活動唯一ID',
        name VARCHAR(100) NOT NULL COMMENT '活動名稱',
        discount_type VARCHAR(20) NOT NULL COMMENT '折扣型態 (PERCENT/FIXED)',
        discount_value DECIMAL(10,2) NOT NULL COMMENT '折扣數值：PERCENT 為百分比、FIXED 為折抵金額',
        starts_at DATETIME NOT NULL COMMENT '活動開始時間',
        ends_at DATETIME NOT NULL COMMENT '活動結束時間',
        is_active TINYINT(1) NOT NULL DEFAULT 1 COMMENT '是否啟用'
    ) COMMENT '促銷活動表';
    """,
    """
    CREATE TABLE IF NOT EXISTS addresses (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '地址唯一ID',
        customer_id INT NOT NULL COMMENT '所屬客戶ID',
        recipient_name VARCHAR(100) NOT NULL COMMENT '收件人姓名（可能不是客戶本人）',
        phone VARCHAR(20) COMMENT '收件人電話',
        city VARCHAR(50) NOT NULL COMMENT '收件城市',
        district VARCHAR(50) COMMENT '收件行政區',
        street VARCHAR(200) COMMENT '收件詳細地址',
        is_default TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否為預設地址',
        created_at DATETIME NOT NULL COMMENT '地址建立時間',
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    ) COMMENT '客戶地址簿';
    """,
    """
    CREATE TABLE IF NOT EXISTS payments (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '付款紀錄唯一ID',
        order_id INT NOT NULL COMMENT '對應訂單ID',
        method_id INT NOT NULL COMMENT '使用的付款方式ID',
        amount DECIMAL(10,2) NOT NULL COMMENT '實際付款金額（可能因促銷低於 orders.total_amount）',
        status VARCHAR(20) NOT NULL COMMENT '付款狀態 (SUCCESS/FAILED/REFUNDED)，與訂單狀態不同意義',
        paid_at DATETIME NOT NULL COMMENT '付款完成時間，必定晚於訂單建立時間',
        transaction_no VARCHAR(64) COMMENT '金流商交易序號',
        sync_version INT NOT NULL DEFAULT 1 COMMENT '內部同步版本號，非業務欄位',
        created_at DATETIME NOT NULL COMMENT '此筆紀錄寫入時間',
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (method_id) REFERENCES payment_methods(id)
    ) COMMENT '付款紀錄表';
    """,
    """
    CREATE TABLE IF NOT EXISTS refunds (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '退款紀錄唯一ID',
        payment_id INT NOT NULL COMMENT '對應的付款紀錄ID',
        amount DECIMAL(10,2) NOT NULL COMMENT '退款金額，可能是部分退款',
        reason VARCHAR(100) COMMENT '退款原因',
        status VARCHAR(20) NOT NULL COMMENT '退款狀態 (REQUESTED/APPROVED/REJECTED/DONE)',
        refunded_at DATETIME NULL COMMENT '實際退款時間，尚未完成時為 NULL',
        created_at DATETIME NOT NULL COMMENT '退款申請時間',
        FOREIGN KEY (payment_id) REFERENCES payments(id)
    ) COMMENT '退款紀錄表';
    """,
    """
    CREATE TABLE IF NOT EXISTS shipments (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '出貨紀錄唯一ID',
        order_id INT NOT NULL COMMENT '對應訂單ID',
        warehouse_id INT NOT NULL COMMENT '出貨倉庫ID',
        tracking_no VARCHAR(64) COMMENT '物流追蹤號碼',
        status VARCHAR(20) NOT NULL COMMENT '物流狀態 (PREPARING/IN_TRANSIT/DELIVERED/RETURNED)，與訂單狀態不同意義',
        shipped_at DATETIME NULL COMMENT '實際出貨時間，尚未出貨時為 NULL',
        delivered_at DATETIME NULL COMMENT '實際送達時間，尚未送達時為 NULL',
        created_at DATETIME NOT NULL COMMENT '出貨單建立時間',
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (warehouse_id) REFERENCES warehouses(id)
    ) COMMENT '出貨紀錄表';
    """,
    """
    CREATE TABLE IF NOT EXISTS reviews (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '評價唯一ID',
        customer_id INT NOT NULL COMMENT '評價者客戶ID',
        product_id INT NOT NULL COMMENT '被評價的商品ID',
        rating TINYINT NOT NULL COMMENT '評分 1 到 5 分',
        comment TEXT COMMENT '評價文字內容',
        is_deleted TINYINT(1) NOT NULL DEFAULT 0 COMMENT '軟刪除標記，1 表示已被刪除不應顯示',
        created_at DATETIME NOT NULL COMMENT '評價時間，必定晚於購買時間',
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (product_id) REFERENCES products(id)
    ) COMMENT '商品評價表（只有買過該商品的客戶才會有評價）';
    """,
    """
    CREATE TABLE IF NOT EXISTS carts (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '購物車唯一ID',
        customer_id INT NOT NULL COMMENT '所屬客戶ID',
        is_abandoned TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否為已放棄的購物車',
        created_at DATETIME NOT NULL COMMENT '購物車建立時間',
        updated_at DATETIME NOT NULL COMMENT '最後異動時間',
        FOREIGN KEY (customer_id) REFERENCES customers(id)
    ) COMMENT '購物車表（尚未結帳，與訂單無關）';
    """,
    """
    CREATE TABLE IF NOT EXISTS cart_items (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '購物車明細唯一ID',
        cart_id INT NOT NULL COMMENT '所屬購物車ID',
        product_id INT NOT NULL COMMENT '商品ID',
        quantity INT NOT NULL COMMENT '放入數量（尚未購買）',
        added_at DATETIME NOT NULL COMMENT '加入購物車的時間',
        FOREIGN KEY (cart_id) REFERENCES carts(id),
        FOREIGN KEY (product_id) REFERENCES products(id)
    ) COMMENT '購物車明細（放入不等於購買）';
    """,
    """
    CREATE TABLE IF NOT EXISTS browse_logs (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '瀏覽紀錄唯一ID',
        customer_id INT NOT NULL COMMENT '瀏覽者客戶ID',
        product_id INT NOT NULL COMMENT '被瀏覽的商品ID',
        viewed_at DATETIME NOT NULL COMMENT '瀏覽時間',
        duration_sec INT NOT NULL COMMENT '停留秒數',
        source VARCHAR(30) COMMENT '流量來源 (SEARCH/RECOMMEND/CATEGORY/AD)',
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (product_id) REFERENCES products(id)
    ) COMMENT '商品瀏覽紀錄（看過不等於買過）';
    """,
    """
    CREATE TABLE IF NOT EXISTS order_promotions (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '唯一ID',
        order_id INT NOT NULL COMMENT '訂單ID',
        promotion_id INT NOT NULL COMMENT '套用的促銷活動ID',
        discount_amount DECIMAL(10,2) NOT NULL COMMENT '此活動實際折抵的金額',
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (promotion_id) REFERENCES promotions(id)
    ) COMMENT '訂單套用的促銷活動';
    """,
    """
    CREATE TABLE IF NOT EXISTS product_categories (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '唯一ID',
        product_id INT NOT NULL COMMENT '商品ID',
        category_id INT NOT NULL COMMENT '分類ID',
        is_primary TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否為主要分類',
        FOREIGN KEY (product_id) REFERENCES products(id),
        FOREIGN KEY (category_id) REFERENCES categories(id)
    ) COMMENT '商品與分類的多對多關聯';
    """,
    """
    CREATE TABLE IF NOT EXISTS product_suppliers (
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '唯一ID',
        product_id INT NOT NULL COMMENT '商品ID',
        supplier_id INT NOT NULL COMMENT '供應商ID',
        supply_price DECIMAL(10,2) NOT NULL COMMENT '進貨價（非售價，售價在 products.price）',
        lead_time_days INT NOT NULL COMMENT '備貨天數',
        FOREIGN KEY (product_id) REFERENCES products(id),
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)
    ) COMMENT '商品供貨關係';
    """,
    """
    CREATE TABLE IF NOT EXISTS product_specs (
        product_id INT PRIMARY KEY COMMENT '商品ID',
        weight_g INT NULL COMMENT '重量（公克）',
        length_mm INT NULL COMMENT '長度（公釐）',
        width_mm INT NULL COMMENT '寬度（公釐）',
        height_mm INT NULL COMMENT '高度（公釐）',
        color VARCHAR(30) NULL COMMENT '顏色',
        material VARCHAR(50) NULL COMMENT '材質',
        origin_country VARCHAR(50) NULL COMMENT '原產地',
        warranty_months INT NULL COMMENT '保固月數',
        power_watt INT NULL COMMENT '功率（瓦），僅電器類有值',
        voltage VARCHAR(20) NULL COMMENT '電壓規格，僅電器類有值',
        battery_mah INT NULL COMMENT '電池容量（mAh），僅 3C 類有值',
        screen_inch DECIMAL(4,1) NULL COMMENT '螢幕尺寸（吋），僅 3C 類有值',
        storage_gb INT NULL COMMENT '儲存容量（GB），僅 3C 類有值',
        ram_gb INT NULL COMMENT '記憶體（GB），僅 3C 類有值',
        connectivity VARCHAR(50) NULL COMMENT '連線規格',
        waterproof_rating VARCHAR(10) NULL COMMENT '防水等級',
        energy_label VARCHAR(10) NULL COMMENT '能源效率標示',
        certification VARCHAR(100) NULL COMMENT '通過的認證',
        package_contents VARCHAR(200) NULL COMMENT '包裝內容物',
        model_no VARCHAR(50) NULL COMMENT '型號',
        release_year INT NULL COMMENT '上市年份',
        is_discontinued TINYINT(1) NOT NULL DEFAULT 0 COMMENT '是否已停產',
        updated_by VARCHAR(50) NULL COMMENT '最後修改者帳號，非業務欄位',
        updated_at DATETIME NULL COMMENT '最後修改時間',
        FOREIGN KEY (product_id) REFERENCES products(id)
    ) COMMENT '商品細部規格（寬表，多數欄位對多數商品為 NULL）';
    """,
]

EXT_TABLES = [
    "product_specs", "product_suppliers", "product_categories", "order_promotions",
    "browse_logs", "cart_items", "carts", "reviews", "shipments", "refunds",
    "payments", "addresses", "promotions", "payment_methods", "warehouses",
    "suppliers", "categories",
]


def create_extended_tables(conn) -> None:
    for ddl in EXT_DDL:
        conn.execute(text(ddl))
    conn.execute(text("SET FOREIGN_KEY_CHECKS = 0;"))
    for table in EXT_TABLES:
        conn.execute(text(f"TRUNCATE TABLE {table};"))
    conn.execute(text("SET FOREIGN_KEY_CHECKS = 1;"))


# ===========================================================================
# 假資料
# ===========================================================================

def _insert(conn, table: str, rows: list[dict]) -> None:
    if not rows:
        return
    cols = list(rows[0])
    conn.execute(
        text(f"INSERT INTO {table} ({', '.join(cols)}) "
             f"VALUES ({', '.join(':' + c for c in cols)})"),
        rows,
    )


def seed_extended(conn) -> dict[str, int]:
    """
    產生擴充表的假資料。

    呼叫端必須確保這個函式在既有 4 張表的資料產生「之後」才執行 ——
    random 是共用序列，插進去就會讓既有資料整份平移。
    """
    anchor = DATA_ANCHOR_DATETIME
    counts: dict[str, int] = {}

    customers = conn.execute(
        text("SELECT id, name, city, created_at FROM customers ORDER BY id")).fetchall()
    products = conn.execute(
        text("SELECT id, name, category, price FROM products ORDER BY id")).fetchall()
    orders = conn.execute(
        text("SELECT id, customer_id, order_date, total_amount, status "
             "FROM orders ORDER BY id")).fetchall()

    cities = ["台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市", "新竹市", "嘉義市"]

    # --- categories：3 個上層 + 6 個葉節點，對應 products.category 的字串 ---
    parents = ["電子與家電", "文化與生活", "服飾與食品"]
    leaves = [("3C數位", 1), ("家電", 1), ("書籍", 2),
              ("生活用品", 2), ("服飾", 3), ("食品", 3)]
    _insert(conn, "categories", [
        {"name": n, "parent_id": None, "sort_order": i, "is_active": 1}
        for i, n in enumerate(parents, 1)])
    _insert(conn, "categories", [
        {"name": n, "parent_id": p, "sort_order": i, "is_active": 1}
        for i, (n, p) in enumerate(leaves, 1)])
    counts["categories"] = len(parents) + len(leaves)
    cat_id = {n: i + len(parents) + 1 for i, (n, _) in enumerate(leaves)}

    # --- suppliers ---
    supplier_names = ["宏達貿易", "永昌實業", "台灣通路", "大新物流商行",
                      "鴻運國際", "金旺企業", "順發供應", "力揚科技"]
    _insert(conn, "suppliers", [
        {"name": n, "contact_email": f"sales{i}@supplier.com.tw",
         "phone": f"02-{random.randint(20000000, 89999999)}",
         "city": random.choice(cities),
         "created_at": anchor - timedelta(days=random.randint(400, 900))}
        for i, n in enumerate(supplier_names, 1)])
    counts["suppliers"] = len(supplier_names)

    # --- warehouses ---
    wh = [("北區倉", "新北市"), ("中區倉", "台中市"),
          ("南區倉", "高雄市"), ("桃園轉運倉", "桃園市")]
    _insert(conn, "warehouses", [
        {"name": n, "city": c, "capacity": random.randint(2000, 20000), "is_active": 1}
        for n, c in wh])
    counts["warehouses"] = len(wh)

    # --- payment_methods ---
    pm = [("信用卡", "CREDIT_CARD", 0.0250), ("貨到付款", "COD", 0.0100),
          ("銀行轉帳", "BANK_TRANSFER", 0.0000), ("行動支付", "MOBILE_PAY", 0.0180),
          ("超商代碼", "CVS_CODE", 0.0300)]
    _insert(conn, "payment_methods", [
        {"name": n, "channel": c, "fee_rate": f, "is_active": 1} for n, c, f in pm])
    counts["payment_methods"] = len(pm)

    # --- promotions ---
    promos = [("春季購物節", "PERCENT", 10), ("滿額折抵", "FIXED", 500),
              ("會員回饋日", "PERCENT", 5), ("清倉特賣", "PERCENT", 20),
              ("新客首購禮", "FIXED", 200), ("週年慶", "PERCENT", 15),
              ("免運補貼", "FIXED", 100), ("夏日限定", "PERCENT", 8)]
    promo_rows = []
    for i, (n, t, v) in enumerate(promos):
        start = anchor - timedelta(days=180 - i * 20)
        promo_rows.append({"name": n, "discount_type": t, "discount_value": v,
                           "starts_at": start, "ends_at": start + timedelta(days=21),
                           "is_active": 1 if i >= 5 else 0})
    _insert(conn, "promotions", promo_rows)
    counts["promotions"] = len(promo_rows)

    # --- addresses：每位客戶 1~3 筆，建立時間晚於註冊 ---
    addr_rows = []
    for cid, cname, ccity, created in customers:
        for k in range(random.randint(1, 3)):
            addr_rows.append({
                "customer_id": cid,
                "recipient_name": cname if k == 0 else
                    random.choice(["王小美", "陳大同", "林先生", "李小姐"]),
                "phone": f"09{random.randint(10000000, 99999999)}",
                "city": ccity if k == 0 else random.choice(cities),
                "district": random.choice(["中正區", "信義區", "北區", "西屯區", "前鎮區"]),
                "street": f"{random.choice(['中山', '民生', '忠孝', '和平'])}路"
                          f"{random.randint(1, 300)}號",
                "is_default": 1 if k == 0 else 0,
                "created_at": created + timedelta(days=random.randint(0, 30)),
            })
    _insert(conn, "addresses", addr_rows)
    counts["addresses"] = len(addr_rows)

    # --- payments：只有非 PENDING / 非 CANCELLED 的訂單才有付款 ---
    pay_rows, pay_of_order = [], {}
    for oid, _cid, odate, amount, status in orders:
        if status in ("PENDING", "CANCELLED"):
            continue
        paid_at = odate + timedelta(hours=random.randint(1, 72))
        pay_rows.append({
            "order_id": oid, "method_id": random.randint(1, len(pm)),
            "amount": float(amount), "status": "SUCCESS", "paid_at": paid_at,
            "transaction_no": f"TX{random.randint(10**11, 10**12 - 1)}",
            "sync_version": random.randint(1, 3), "created_at": paid_at,
        })
        pay_of_order[oid] = (len(pay_rows), paid_at, float(amount))
    _insert(conn, "payments", pay_rows)
    counts["payments"] = len(pay_rows)

    # --- refunds：只針對已成功付款的訂單，退款晚於付款 ---
    refund_rows = []
    for oid in random.sample(sorted(pay_of_order), 18):
        pid, paid_at, amount = pay_of_order[oid]
        status = random.choices(["DONE", "APPROVED", "REQUESTED", "REJECTED"],
                                weights=[60, 15, 15, 10])[0]
        created = paid_at + timedelta(days=random.randint(1, 40))
        refund_rows.append({
            "payment_id": pid,
            "amount": round(amount * random.choice([1.0, 0.5, 0.3]), 2),
            "reason": random.choice(["商品瑕疵", "尺寸不合", "不想要了", "送錯商品", "延遲到貨"]),
            "status": status,
            "refunded_at": created + timedelta(days=random.randint(1, 7))
                           if status == "DONE" else None,
            "created_at": created,
        })
    _insert(conn, "refunds", refund_rows)
    counts["refunds"] = len(refund_rows)

    # --- shipments：SHIPPED / COMPLETED 的訂單才會出貨 ---
    ship_rows = []
    for oid, _cid, odate, _amt, status in orders:
        if status not in ("SHIPPED", "COMPLETED"):
            continue
        base = pay_of_order.get(oid, (None, odate, None))[1]
        shipped = base + timedelta(days=random.randint(1, 5))
        delivered = shipped + timedelta(days=random.randint(1, 6)) \
            if status == "COMPLETED" else None
        ship_rows.append({
            "order_id": oid, "warehouse_id": random.randint(1, len(wh)),
            "tracking_no": f"TW{random.randint(10**9, 10**10 - 1)}",
            "status": "DELIVERED" if status == "COMPLETED" else "IN_TRANSIT",
            "shipped_at": shipped, "delivered_at": delivered,
            "created_at": base,
        })
    _insert(conn, "shipments", ship_rows)
    counts["shipments"] = len(ship_rows)

    # --- reviews：只有買過該商品的客戶才能評價，評價晚於下單 ---
    bought = conn.execute(text(
        "SELECT DISTINCT o.customer_id, oi.product_id, o.order_date "
        "FROM orders o JOIN order_items oi ON o.id = oi.order_id "
        "WHERE o.status IN ('COMPLETED', 'SHIPPED') ORDER BY 1, 2")).fetchall()
    review_rows = []
    for cid, pid, odate in bought:
        if random.random() > 0.35:
            continue
        review_rows.append({
            "customer_id": cid, "product_id": pid,
            "rating": random.choices([5, 4, 3, 2, 1], weights=[40, 30, 15, 10, 5])[0],
            "comment": random.choice([
                "品質不錯，會回購", "普通，跟描述有一點落差", "物超所值！",
                "出貨很快，包裝完整", "有點失望", "推薦給大家", "還可以接受"]),
            "is_deleted": 1 if random.random() < 0.06 else 0,
            "created_at": odate + timedelta(days=random.randint(3, 45)),
        })
    _insert(conn, "reviews", review_rows)
    counts["reviews"] = len(review_rows)

    # --- carts / cart_items：與訂單無關的「尚未購買」 ---
    cart_rows, cart_owner = [], []
    for _ in range(70):
        cid, _n, _c, created = random.choice(customers)
        made = anchor - timedelta(days=random.randint(1, 120))
        made = max(made, created)
        cart_rows.append({"customer_id": cid,
                          "is_abandoned": 1 if random.random() < 0.55 else 0,
                          "created_at": made,
                          "updated_at": made + timedelta(days=random.randint(0, 10))})
        cart_owner.append(made)
    _insert(conn, "carts", cart_rows)
    counts["carts"] = len(cart_rows)

    ci_rows = []
    for cart_idx, made in enumerate(cart_owner, 1):
        for prod in random.sample(products, random.randint(1, 4)):
            ci_rows.append({"cart_id": cart_idx, "product_id": prod[0],
                            "quantity": random.randint(1, 3),
                            "added_at": made + timedelta(hours=random.randint(0, 48))})
    _insert(conn, "cart_items", ci_rows)
    counts["cart_items"] = len(ci_rows)

    # --- browse_logs：語意捷徑的來源。看過的商品遠多於買過的 ---
    bl_rows = []
    for _ in range(900):
        cid, _n, _c, created = random.choice(customers)
        prod = random.choice(products)
        viewed = anchor - timedelta(days=random.randint(1, 170),
                                    hours=random.randint(0, 23))
        if viewed < created:
            continue
        bl_rows.append({"customer_id": cid, "product_id": prod[0], "viewed_at": viewed,
                        "duration_sec": random.randint(3, 600),
                        "source": random.choice(["SEARCH", "RECOMMEND", "CATEGORY", "AD"])})
    _insert(conn, "browse_logs", bl_rows)
    counts["browse_logs"] = len(bl_rows)

    # --- order_promotions ---
    op_rows = []
    for oid, _cid, odate, amount, _s in orders:
        if random.random() > 0.22:
            continue
        pidx = random.randint(1, len(promos))
        ptype, pval = promos[pidx - 1][1], promos[pidx - 1][2]
        disc = round(float(amount) * pval / 100, 2) if ptype == "PERCENT" else float(pval)
        op_rows.append({"order_id": oid, "promotion_id": pidx,
                        "discount_amount": min(disc, float(amount))})
    _insert(conn, "order_promotions", op_rows)
    counts["order_promotions"] = len(op_rows)

    # --- product_categories：正規化版本，與 products.category 並存 ---
    pc_rows = []
    for pid, _n, category, _p in products:
        pc_rows.append({"product_id": pid, "category_id": cat_id[category], "is_primary": 1})
        if random.random() < 0.2:
            other = random.choice([v for k, v in cat_id.items() if k != category])
            pc_rows.append({"product_id": pid, "category_id": other, "is_primary": 0})
    _insert(conn, "product_categories", pc_rows)
    counts["product_categories"] = len(pc_rows)

    # --- product_suppliers：supply_price 一定低於售價 ---
    ps_rows = []
    for pid, _n, _c, price in products:
        for sid in random.sample(range(1, len(supplier_names) + 1), random.randint(1, 2)):
            ps_rows.append({"product_id": pid, "supplier_id": sid,
                            "supply_price": round(float(price) * random.uniform(0.45, 0.75), 2),
                            "lead_time_days": random.randint(1, 30)})
    _insert(conn, "product_suppliers", ps_rows)
    counts["product_suppliers"] = len(ps_rows)

    # --- product_specs：寬表，欄位依類別大量留白 ---
    spec_rows = []
    for pid, _n, category, _p in products:
        is_3c, is_appliance = category == "3C數位", category == "家電"
        spec_rows.append({
            "product_id": pid,
            "weight_g": random.randint(50, 15000),
            "length_mm": random.randint(50, 800), "width_mm": random.randint(30, 600),
            "height_mm": random.randint(10, 500),
            "color": random.choice(["黑", "白", "銀", "藍", "灰"]),
            "material": random.choice(["塑膠", "金屬", "玻璃", "紙", "布"]),
            "origin_country": random.choice(["台灣", "中國", "越南", "日本", "泰國"]),
            "warranty_months": random.choice([0, 6, 12, 24]),
            "power_watt": random.randint(50, 2000) if is_appliance else None,
            "voltage": "110V" if is_appliance else None,
            "battery_mah": random.randint(2000, 6000) if is_3c else None,
            "screen_inch": round(random.uniform(4.7, 16.0), 1) if is_3c else None,
            "storage_gb": random.choice([64, 128, 256, 512]) if is_3c else None,
            "ram_gb": random.choice([4, 8, 16]) if is_3c else None,
            "connectivity": "Wi-Fi/藍牙" if (is_3c or is_appliance) else None,
            "waterproof_rating": random.choice(["IPX4", "IP67", None]),
            "energy_label": random.choice(["1級", "2級", "3級"]) if is_appliance else None,
            "certification": "BSMI" if (is_3c or is_appliance) else None,
            "package_contents": "主機、說明書、保固卡",
            "model_no": f"MD-{pid:03d}-{random.randint(100, 999)}",
            "release_year": random.randint(2022, 2026),
            "is_discontinued": 1 if random.random() < 0.1 else 0,
            "updated_by": random.choice(["admin", "editor01", "sys_batch"]),
            "updated_at": anchor - timedelta(days=random.randint(1, 200)),
        })
    _insert(conn, "product_specs", spec_rows)
    counts["product_specs"] = len(spec_rows)

    return counts
