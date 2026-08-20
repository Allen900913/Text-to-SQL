"""規模第二波：再加 20 張表，把資料庫從 60 張推到 80 張

為什麼第一波之後還要再加（ARCHITECTURE.md §11.5）：

    60 張時 CANDIDATE_N = 40 砍掉三分之一，候選只涵蓋 67%，
    而漏斗召回只從 99.3% 掉到 98.6% —— 幾乎沒退化。
    同一份題目下純相似度 K=3 卻從 91.4% 崩到 85.5%，兩者差距 8pp → 13pp。

    也就是說 60 張還不足以壓出漏斗的極限，只證明了「純相似度會先死」。
    80 張時候選只涵蓋 50%，第一段要丟掉一半的表 —— 那才是真正的壓力測試。

比例同 §10.2：16 張業務表（有資料、有 GT）+ 4 張基礎設施表（有資料、
never_answered）= 8:2。

連通性同第一波：每張業務表都有 FK 連回既有骨幹。這一波刻意補上
**組織 ↔ 場域**的連結（warehouse_staff / store_staff 把 employees 接到
warehouses 與 stores），讓「哪個倉庫的人力最多」這種跨域問題有路可走。

刻意迴避的欄位（第三次重申，這份清單每加一波就要重讀一次）：

    #68  運費      —— return_shipments 只有單號與時間，沒有任何費用欄位
    #70  進貨成本  —— 本波不加 purchase_orders，因為採購單沒有單價很怪，
                      有單價又會讓「利潤率」變成可算的（product_suppliers
                      .supply_price 已經是 #70 的 acknowledged 邊緣案例）
    #140 在庫量    —— 不加 stock_alerts / back_in_stock，表名就會命中 stock 探針
    #141 評分      —— customer_feedback 用 sentiment 列舉值，不用 rating 分數

用法：
    python tools/add_domain_tables_w2.py            # 預覽
    python tools/add_domain_tables_w2.py --apply    # 建表（不寫入任何資料）
    python tools/add_domain_tables_w2.py --drop     # 全部移除，回到 60 張
"""
import os
import sys

from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

DOMAIN_TABLES = [
    # ---------------- 組織 ↔ 場域 ----------------
    ("warehouse_staff", "倉庫人員配置：哪位員工被派駐在哪個倉庫。員工主檔在 employees，倉庫主檔在 warehouses", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '配置唯一ID',
        warehouse_id INT NOT NULL COMMENT '倉庫ID',
        employee_id INT NOT NULL COMMENT '員工ID',
        assigned_at DATE COMMENT '派駐起始日',
        FOREIGN KEY (warehouse_id) REFERENCES warehouses(id),
        FOREIGN KEY (employee_id) REFERENCES employees(id)"""),

    ("store_staff", "門市人員配置：哪位員工在哪間門市服務。這是實體門市的人力，倉庫人力在 warehouse_staff", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '配置唯一ID',
        store_id INT NOT NULL COMMENT '門市ID',
        employee_id INT NOT NULL COMMENT '員工ID',
        assigned_at DATE COMMENT '派駐起始日',
        FOREIGN KEY (store_id) REFERENCES stores(id),
        FOREIGN KEY (employee_id) REFERENCES employees(id)"""),

    ("employee_shifts", "員工班表：每位員工每天的班別與工時。這是排班不是出勤紀錄", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '班表唯一ID',
        employee_id INT NOT NULL COMMENT '員工ID',
        shift_date DATE COMMENT '班表日期',
        shift_type VARCHAR(20) COMMENT '班別 (MORNING/AFTERNOON/NIGHT)',
        hours DECIMAL(4,1) COMMENT '排定工時',
        FOREIGN KEY (employee_id) REFERENCES employees(id)"""),

    # ---------------- 商品變體 ----------------
    ("product_variants", "商品規格變體：同一款商品的顏色與尺寸組合。商品主檔在 products，規格說明在 product_specs", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '變體唯一ID',
        product_id INT NOT NULL COMMENT '所屬商品ID',
        color VARCHAR(20) COMMENT '顏色',
        size VARCHAR(20) COMMENT '尺寸',
        is_default TINYINT(1) DEFAULT 0 COMMENT '是否為預設變體',
        FOREIGN KEY (product_id) REFERENCES products(id)"""),

    ("variant_barcodes", "變體條碼：每個規格變體對應的國際條碼。一個變體可能有多組條碼（改版）", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '條碼唯一ID',
        variant_id INT NOT NULL COMMENT '變體ID',
        barcode VARCHAR(20) COMMENT '條碼字串',
        registered_at DATE COMMENT '登錄日期',
        FOREIGN KEY (variant_id) REFERENCES product_variants(id)"""),

    # ---------------- 行銷 ----------------
    ("newsletters", "電子報主檔：每一期電子報的主旨與寄送時間。這是群發信，購物車挽回信在 cart_recovery_emails", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '電子報唯一ID',
        subject VARCHAR(100) COMMENT '電子報主旨',
        sent_at DATETIME COMMENT '寄送時間'"""),

    ("newsletter_subscriptions", "電子報訂閱：哪些客戶訂閱了電子報、是否已退訂。這是收信意願，付費訂閱方案在 subscriptions", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '訂閱唯一ID',
        customer_id INT NOT NULL COMMENT '客戶ID',
        subscribed_at DATETIME COMMENT '訂閱時間',
        unsubscribed_at DATETIME COMMENT '退訂時間，仍訂閱中為空',
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),

    ("campaigns", "行銷活動：廣告投放的活動名稱、渠道與期間。這是廣告活動，站上折扣檔期在 promotions", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '活動唯一ID',
        name VARCHAR(50) COMMENT '活動名稱',
        channel VARCHAR(20) COMMENT '投放渠道 (EMAIL/SOCIAL/SEARCH/DISPLAY)',
        started_at DATE COMMENT '開始日期',
        ended_at DATE COMMENT '結束日期'"""),

    ("campaign_clicks", "廣告點擊紀錄：哪位客戶點了哪個活動的廣告。點擊不是購買，銷售請看 order_items", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '點擊唯一ID',
        campaign_id INT NOT NULL COMMENT '活動ID',
        customer_id INT COMMENT '客戶ID，未登入為空',
        clicked_at DATETIME COMMENT '點擊時間',
        FOREIGN KEY (campaign_id) REFERENCES campaigns(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),

    ("search_logs", "站內搜尋紀錄：客戶輸入的關鍵字與命中筆數。搜尋不是瀏覽也不是購買，瀏覽在 browse_logs", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '搜尋紀錄唯一ID',
        customer_id INT COMMENT '客戶ID，未登入為空',
        keyword VARCHAR(50) COMMENT '搜尋關鍵字',
        result_count INT COMMENT '命中筆數',
        searched_at DATETIME COMMENT '搜尋時間',
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),

    ("product_recommendations", "商品推薦關聯：系統為某商品推薦的其他商品。這是演算法推薦，不是實際的共同購買紀錄", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '推薦唯一ID',
        product_id INT NOT NULL COMMENT '來源商品ID',
        recommended_product_id INT NOT NULL COMMENT '被推薦的商品ID',
        rank_no INT COMMENT '推薦排序，1 為最前',
        FOREIGN KEY (product_id) REFERENCES products(id),
        FOREIGN KEY (recommended_product_id) REFERENCES products(id)"""),

    # ---------------- 客服與售後 ----------------
    ("faq_articles", "常見問題文章：標題、分類與發布時間。這是自助說明文件，客戶提出的工單在 support_tickets", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '文章唯一ID',
        title VARCHAR(100) COMMENT '文章標題',
        category VARCHAR(30) COMMENT '文章分類',
        published_at DATE COMMENT '發布日期'"""),

    ("faq_votes", "常見問題投票：客戶對文章按有幫助或沒幫助。這不是商品評價，商品評價在 reviews", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '投票唯一ID',
        article_id INT NOT NULL COMMENT '文章ID',
        customer_id INT COMMENT '投票客戶ID，未登入為空',
        is_helpful TINYINT(1) COMMENT '是否有幫助',
        voted_at DATETIME COMMENT '投票時間',
        FOREIGN KEY (article_id) REFERENCES faq_articles(id),
        FOREIGN KEY (customer_id) REFERENCES customers(id)"""),

    ("return_shipments", "退貨物流：客戶把商品寄回時的物流單號與收件時間。退貨申請在 order_returns，退款在 refunds", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '退貨物流唯一ID',
        return_id INT NOT NULL COMMENT '對應退貨申請ID',
        tracking_no VARCHAR(30) COMMENT '物流單號',
        shipped_at DATETIME COMMENT '客戶寄出時間',
        received_at DATETIME COMMENT '倉庫收到時間，尚未收到為空',
        FOREIGN KEY (return_id) REFERENCES order_returns(id)"""),

    ("warranty_claims", "保固申請：客戶對已購商品提出的保固維修申請與處理狀態。這不是退貨，退貨在 order_returns", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '保固申請唯一ID',
        order_id INT NOT NULL COMMENT '購買時的訂單ID',
        product_id INT NOT NULL COMMENT '申請保固的商品ID',
        status VARCHAR(20) COMMENT '處理狀態 (SUBMITTED/APPROVED/REJECTED/DONE)',
        claimed_at DATETIME COMMENT '申請時間',
        FOREIGN KEY (order_id) REFERENCES orders(id),
        FOREIGN KEY (product_id) REFERENCES products(id)"""),

    ("service_appointments", "到店服務預約：客戶預約到哪間門市、什麼時間、做什麼服務。這是預約不是取貨，取貨在 store_pickups", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '預約唯一ID',
        customer_id INT NOT NULL COMMENT '預約客戶ID',
        store_id INT NOT NULL COMMENT '預約門市ID',
        service_type VARCHAR(30) COMMENT '服務類型 (REPAIR/CONSULT/INSTALL)',
        scheduled_at DATETIME COMMENT '預約時間',
        attended TINYINT(1) COMMENT '是否實際到場',
        FOREIGN KEY (customer_id) REFERENCES customers(id),
        FOREIGN KEY (store_id) REFERENCES stores(id)"""),

    # ---------------- 基礎設施（never_answered）----------------
    ("api_request_logs", "API 請求日誌：每個對外 API 呼叫的路徑、狀態碼與耗時。系統維運用，不是業務資料", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '請求唯一ID',
        path VARCHAR(100) COMMENT '請求路徑',
        status_code INT COMMENT 'HTTP 狀態碼',
        duration_ms INT COMMENT '耗時毫秒',
        requested_at DATETIME COMMENT '請求時間'"""),

    ("feature_flags", "功能開關：每個功能旗標目前是否開啟。工程團隊維護，不是業務資料", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '旗標唯一ID',
        flag_key VARCHAR(50) COMMENT '旗標名稱',
        is_enabled TINYINT(1) COMMENT '是否啟用',
        updated_at DATETIME COMMENT '最後更新時間'"""),

    ("cache_entries", "快取項目：應用層快取的鍵與到期時間。系統自動維護，不是業務資料", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '快取唯一ID',
        cache_key VARCHAR(100) COMMENT '快取鍵',
        expires_at DATETIME COMMENT '到期時間',
        hit_count INT COMMENT '命中次數'"""),

    ("error_reports", "錯誤回報：系統例外的類型、訊息與發生時間。工程除錯用，不是業務資料", """
        id INT AUTO_INCREMENT PRIMARY KEY COMMENT '錯誤唯一ID',
        error_type VARCHAR(50) COMMENT '例外類型',
        message VARCHAR(200) COMMENT '錯誤訊息',
        occurred_at DATETIME COMMENT '發生時間'"""),
]

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
    print(f"現有 {len(present)} 張表；這一波要加 {len(todo)} 張")
    for name, brief, _cols in DOMAIN_TABLES:
        tag = "" if name in todo else "  （已存在，跳過）"
        print(f"  {name:<26}{brief[:44]}{tag}")

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
    print("\n下一步：灌資料 → tools/check_schema_pipeline.py 七項全綠")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
