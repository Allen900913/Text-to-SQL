"""規模實驗：加 19 張干擾表，把資料庫從 22 張推到 41 張（跨過 CANDIDATE_N）

為什麼是 41 不是 100（ARCHITECTURE.md §7.2、§7.3）：

    CANDIDATE_N = 40，而現在只有 22 張表 —— 第一段漏斗**從來沒有執行過**。
    表數一過 40，那一段立刻從死碼變成關鍵路徑，用的還是三層裡唯一會隨規模
    退化的相似度。22 → 100 一步跳過去會同時觸發四五件事，一件都歸因不了。
    真正資訊量最大的一步是 40 → 41。

為什麼可以不補一題 GT：

    檢索評估只讀 INFORMATION_SCHEMA，不讀資料。所以干擾表可以是
    **純 schema、零資料列**：
      · 不必補 GT —— 指標是「既有 159 題的召回有沒有掉」，新表全是干擾物
      · 不碰種子資料 —— 只有 CREATE TABLE，連 INSERT 都沒有
      · 可逆 —— --drop 就回到 22 張表
    代價是端到端測不了（沒資料就沒答案），但規模問題本來就發生在檢索層。

為什麼干擾表必須「像」：

    §7.1 那個實驗是用「隨機抽既有表」模擬干擾。真實 schema 的困難在於
    **近義表** —— order_status_history / order_notes / order_cancellations
    這種。隨便生 19 張不相干的表會低估退化。所以下面每一張都刻意貼著
    既有的表做，表註解也用同樣的語氣寫。

刻意迴避的東西：

    不加「運費」「利潤」「週轉率」「信用評分」相關的欄位 —— 那會讓
    #68 / #70 / #140 / #141 這四題防禦題失效（§8.8）。加完會自動跑
    tools/check_defence_gt.py 確認。

用法：
    python tools/add_distractor_tables.py            # 預覽
    python tools/add_distractor_tables.py --apply    # 建表（不寫入任何資料）
    python tools/add_distractor_tables.py --drop     # 全部移除，回到 22 張
"""
import os
import sys

from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

# 每一張都貼著既有的表，語意上刻意容易混淆。
# (表名, 表註解, 欄位 DDL)
DISTRACTORS: list[tuple[str, str, str]] = [
    ("order_status_history", "訂單狀態異動歷程：每次狀態變更留一筆，記錄前後狀態與異動時間。想看訂單「現在」的狀態請用 orders.status", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '異動紀錄唯一ID',
        order_id INT NOT NULL COMMENT '所屬訂單ID',
        from_status VARCHAR(20) COMMENT '異動前狀態',
        to_status VARCHAR(20) NOT NULL COMMENT '異動後狀態',
        changed_at DATETIME NOT NULL COMMENT '狀態變更時間',
        changed_by VARCHAR(50) COMMENT '異動者帳號，非業務欄位',
        FOREIGN KEY (order_id) REFERENCES orders(id)"""),
    ("order_notes", "訂單備註：客服或客戶在訂單上留的文字備註，與商品評論無關", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '備註唯一ID',
        order_id INT NOT NULL COMMENT '所屬訂單ID',
        note TEXT COMMENT '備註內容',
        is_internal TINYINT(1) DEFAULT 0 COMMENT '是否為內部備註，客戶看不到',
        created_at DATETIME COMMENT '備註時間',
        FOREIGN KEY (order_id) REFERENCES orders(id)"""),
    ("order_cancellations", "訂單取消紀錄：取消的原因與時間。訂單是否已取消請看 orders.status = 'CANCELLED'", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '取消紀錄唯一ID',
        order_id INT NOT NULL COMMENT '被取消的訂單ID',
        reason VARCHAR(100) COMMENT '取消原因',
        cancelled_by VARCHAR(20) COMMENT '取消發起方 (CUSTOMER/SYSTEM/STAFF)',
        cancelled_at DATETIME COMMENT '取消時間',
        FOREIGN KEY (order_id) REFERENCES orders(id)"""),
    ("order_returns", "退貨申請單：客戶寄回商品的申請與處理狀態。退款金額在 refunds，這裡只管實體退貨", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '退貨單唯一ID',
        order_id INT NOT NULL COMMENT '對應訂單ID',
        product_id INT COMMENT '退回的商品ID',
        quantity INT COMMENT '退回數量',
        status VARCHAR(20) COMMENT '退貨狀態 (REQUESTED/RECEIVED/REJECTED)',
        requested_at DATETIME COMMENT '申請時間',
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (product_id) REFERENCES products(id)"""),
    ("payment_attempts", "付款嘗試紀錄：每次刷卡嘗試都留一筆，含失敗的。成功的付款結果在 payments", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '嘗試紀錄唯一ID',
        order_id INT NOT NULL COMMENT '對應訂單ID',
        method_id INT COMMENT '嘗試使用的付款方式ID',
        result VARCHAR(20) COMMENT '嘗試結果：OK 成功；DECLINED 銀行拒絕、TIMEOUT 逾時未回應，這兩者都算失敗',
        attempted_at DATETIME COMMENT '嘗試時間',
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (method_id) REFERENCES payment_methods(id)"""),
    ("invoices", "發票開立紀錄：發票號碼、開立時間與載具。實際收款金額在 payments", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '發票唯一ID',
        order_id INT NOT NULL COMMENT '對應訂單ID',
        invoice_no VARCHAR(20) COMMENT '發票號碼',
        carrier VARCHAR(30) COMMENT '載具號碼',
        issued_at DATETIME COMMENT '開立時間',
        is_voided TINYINT(1) DEFAULT 0 COMMENT '是否已作廢',
        FOREIGN KEY (order_id) REFERENCES orders(id)"""),
    ("delivery_attempts", "配送嘗試紀錄：每一次上門派送留一筆，含撲空。最終是否送達看 shipments.status", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '派送紀錄唯一ID',
        shipment_id INT NOT NULL COMMENT '所屬出貨單ID',
        attempt_no INT COMMENT '第幾次嘗試',
        result VARCHAR(20) COMMENT '派送結果 (DELIVERED/ABSENT/REFUSED)',
        attempted_at DATETIME COMMENT '派送時間',
        FOREIGN KEY (shipment_id) REFERENCES shipments(id)"""),
    ("warehouse_transfers", "倉庫調撥單：商品在兩個倉庫之間移動的紀錄，不是出貨給客戶", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '調撥單唯一ID',
        from_warehouse_id INT COMMENT '來源倉庫ID',
        to_warehouse_id INT COMMENT '目的倉庫ID',
        product_id INT COMMENT '調撥商品ID',
        quantity INT COMMENT '調撥數量',
        transferred_at DATETIME COMMENT '調撥時間',
        FOREIGN KEY (from_warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY (to_warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY (product_id) REFERENCES products(id)"""),
    ("product_price_history", "商品售價異動歷程：每次調價留一筆。商品「現在」的售價在 products.price", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '調價紀錄唯一ID',
        product_id INT NOT NULL COMMENT '商品ID',
        old_price DECIMAL(10,2) COMMENT '調整前售價',
        new_price DECIMAL(10,2) COMMENT '調整後售價',
        changed_at DATETIME COMMENT '調價時間',
        FOREIGN KEY (product_id) REFERENCES products(id)"""),
    ("product_stock_snapshots", "商品庫存每日快照：每天結算一次的庫存數。即時庫存在 products.stock", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '快照唯一ID',
        product_id INT NOT NULL COMMENT '商品ID',
        snapshot_date DATE COMMENT '快照日期',
        stock_qty INT COMMENT '該日結算的庫存數',
        FOREIGN KEY (product_id) REFERENCES products(id)"""),
    ("product_images", "商品圖片：主圖與附圖的檔案路徑與排序", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '圖片唯一ID',
        product_id INT NOT NULL COMMENT '商品ID',
        url VARCHAR(200) COMMENT '圖片路徑',
        is_primary TINYINT(1) DEFAULT 0 COMMENT '是否為主圖',
        sort_order INT COMMENT '顯示順序',
        FOREIGN KEY (product_id) REFERENCES products(id)"""),
    ("product_bundles", "商品組合包：一個組合包由多個商品構成，例如超值三入組", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '組合關係唯一ID',
        bundle_product_id INT NOT NULL COMMENT '組合包本身的商品ID',
        item_product_id INT NOT NULL COMMENT '包含的單品商品ID',
        quantity INT COMMENT '包含幾件',
        FOREIGN KEY (bundle_product_id) REFERENCES products(id),
        FOREIGN KEY (item_product_id) REFERENCES products(id)"""),
    ("customer_contacts", "客戶聯絡方式：客戶登記的其他電話與信箱。主要聯絡方式在 customers", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '聯絡方式唯一ID',
        customer_id INT NOT NULL COMMENT '所屬客戶ID',
        contact_type VARCHAR(20) COMMENT '類型 (PHONE/EMAIL/LINE)',
        contact_value VARCHAR(100) COMMENT '聯絡內容',
        is_verified TINYINT(1) DEFAULT 0 COMMENT '是否已驗證',
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),
    ("customer_login_logs", "客戶登入紀錄：每次登入留一筆，含失敗。最近一次登入時間在 customer_profiles.last_login_at", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '登入紀錄唯一ID',
        customer_id INT NOT NULL COMMENT '登入的客戶ID',
        logged_in_at DATETIME COMMENT '登入時間',
        ip VARCHAR(45) COMMENT '來源IP，非業務欄位',
        success TINYINT(1) COMMENT '是否登入成功',
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),
    ("support_tickets", "客服工單：客戶提出的問題與處理狀態，與商品評價無關", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '工單唯一ID',
        customer_id INT NOT NULL COMMENT '提出的客戶ID',
        order_id INT COMMENT '相關訂單ID，可為空',
        subject VARCHAR(100) COMMENT '問題主旨',
        status VARCHAR(20) COMMENT '工單狀態 (OPEN/PENDING/CLOSED)',
        created_at DATETIME COMMENT '建立時間',
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (order_id) REFERENCES orders(id)"""),
    ("review_replies", "評論回覆：商家對客戶評論的回應內容", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '回覆唯一ID',
        review_id INT NOT NULL COMMENT '被回覆的評論ID',
        reply_text TEXT COMMENT '回覆內容',
        replied_at DATETIME COMMENT '回覆時間',
        FOREIGN KEY (review_id) REFERENCES reviews(id)"""),
    ("cart_recovery_emails", "購物車挽回信：對放棄購物車的客戶寄出的提醒信與開信結果", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '寄送紀錄唯一ID',
        cart_id INT NOT NULL COMMENT '對應購物車ID',
        sent_at DATETIME COMMENT '寄送時間',
        opened TINYINT(1) DEFAULT 0 COMMENT '是否開信',
        clicked TINYINT(1) DEFAULT 0 COMMENT '是否點擊',
        FOREIGN KEY (cart_id) REFERENCES carts(id)"""),
    ("promotion_rules", "促銷活動的適用條件：門檻金額、可疊加與否等。折扣數值本身在 promotions", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '規則唯一ID',
        promotion_id INT NOT NULL COMMENT '所屬促銷活動ID',
        min_amount DECIMAL(10,2) COMMENT '最低消費門檻',
        stackable TINYINT(1) DEFAULT 0 COMMENT '是否可與其他活動疊加',
        applies_to VARCHAR(30) COMMENT '適用範圍 (ALL/CATEGORY/PRODUCT)',
        FOREIGN KEY (promotion_id) REFERENCES promotions(id)"""),
    ("supplier_contracts", "供應商合約：合約期間與付款條件。單一商品的進貨價在 product_suppliers", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '合約唯一ID',
        supplier_id INT NOT NULL COMMENT '供應商ID',
        contract_no VARCHAR(30) COMMENT '合約編號',
        starts_at DATE COMMENT '合約起始日',
        ends_at DATE COMMENT '合約到期日',
        payment_terms VARCHAR(30) COMMENT '付款條件，例如月結30天',
        FOREIGN KEY (supplier_id) REFERENCES suppliers(id)"""),
]

BASE_TABLES = 22


def existing(conn) -> set[str]:
    return {r[0].lower() for r in conn.execute(text(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE()")).fetchall()}


def main() -> int:
    apply_it = "--apply" in sys.argv
    drop_it = "--drop" in sys.argv
    db = get_db_manager(MYSQL_URI)
    names = [n for n, _, _ in DISTRACTORS]

    with db.engine.connect() as conn:
        present = existing(conn)
    here = [n for n in names if n in present]
    print(f"資料庫現有 {len(present)} 張表；干擾表 {len(names)} 張，"
          f"其中 {len(here)} 張已存在\n")

    if drop_it:
        # 反向刪除，先建的後刪 —— 外鍵只從干擾表指向既有表，彼此無依賴，
        # 但 product_bundles 之類自我參照的表仍以反序最安全
        with db.engine.begin() as conn:
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
            for name in reversed(names):
                conn.execute(text(f"DROP TABLE IF EXISTS {name}"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        with db.engine.connect() as conn:
            after = existing(conn)
        print(f"已移除。現在 {len(after)} 張表"
              f"{'（回到基準）' if len(after) == BASE_TABLES else ''}")
        return 0

    if not apply_it:
        for name, brief, _ in DISTRACTORS:
            mark = "（已存在）" if name in present else ""
            print(f"  {name:<26}{mark}\n      {brief[:56]}…")
        print(f"\n加完會是 {len(present | set(names))} 張表。"
              f"只建表、不寫入任何資料列。加 --apply 才會執行。")
        return 0

    with db.engine.begin() as conn:
        for name, brief, cols in DISTRACTORS:
            if name in present:
                continue
            # 表註解裡有 'CANCELLED' 這種單引號，不跳脫會截斷字串字面值
            conn.execute(text(
                f"CREATE TABLE {name} ({cols.strip()}) "
                f"ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 "
                f"COMMENT='{brief.replace(chr(39), chr(39) * 2)}'"))
    with db.engine.connect() as conn:
        after = existing(conn)
        n_cols = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()")).scalar()
        rows = conn.execute(text(
            "SELECT SUM(TABLE_ROWS) FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME IN :n"
        ), {"n": tuple(names)}).scalar()
    print(f"已建立。現在 {len(after)} 張表 / {n_cols} 欄；"
          f"干擾表資料列數 = {rows or 0}（應為 0）")
    print("\n下一步:")
    print("  python tools/check_defence_gt.py      # 新表有沒有讓防禦題失效")
    print("  python eval/eval_retrieval.py --funnel     # 跨過 CANDIDATE_N 之後的召回")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
