import random
from datetime import timedelta
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError

# 固定種子與固定時間錨點：重跑這支程式必須產生位元等價的資料庫，
# 否則預期答案（ground truth）沒辦法進版控。理由詳見 data_anchor 模組。
from langgraph_sql.data_anchor import (
    DATA_ANCHOR_DATE,
    DATA_ANCHOR_DATETIME,
    DATA_RANDOM_SEED,
)

# 使用 root 權限連線到本地 MySQL，建立 ecommerce_demo 資料庫
# 根據圖片 Port 為 3306，主機為 127.0.0.1
# 若你的 MySQL 帳密不同，請自行修改
DB_URI_ROOT = "mysql+pymysql://root:123456@127.0.0.1:3306/"
DB_NAME = "ecommerce_demo"

def init_database():
    try:
        # 連線至預設資料庫以建立新資料庫
        engine_root = create_engine(DB_URI_ROOT)
        with engine_root.connect() as conn:
            conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {DB_NAME} DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
            print(f"資料庫 {DB_NAME} 建立/確認成功")

        # 連線至剛建立的資料庫
        engine = create_engine(f"{DB_URI_ROOT}{DB_NAME}")
        
        with engine.connect() as conn:
            # 建立 customers 表
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS customers (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '客戶唯一ID',
                    name VARCHAR(100) NOT NULL COMMENT '客戶姓名',
                    email VARCHAR(100) NOT NULL COMMENT '客戶信箱',
                    phone VARCHAR(20) COMMENT '客戶電話',
                    city VARCHAR(50) COMMENT '居住城市',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP COMMENT '註冊時間'
                ) COMMENT '客戶資訊表';
            """))

            # 建立 products 表
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS products (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '商品唯一ID',
                    name VARCHAR(100) NOT NULL COMMENT '商品名稱',
                    category VARCHAR(50) NOT NULL COMMENT '商品分類',
                    price DECIMAL(10, 2) NOT NULL COMMENT '商品價格',
                    stock INT NOT NULL DEFAULT 0 COMMENT '庫存數量',
                    description TEXT COMMENT '商品描述'
                ) COMMENT '商品資訊表';
            """))

            # 建立 orders 表
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '訂單唯一ID',
                    customer_id INT NOT NULL COMMENT '購買客戶ID',
                    order_date DATETIME NOT NULL COMMENT '訂單建立時間',
                    total_amount DECIMAL(10, 2) NOT NULL COMMENT '訂單總金額',
                    status VARCHAR(20) NOT NULL DEFAULT 'PENDING' COMMENT '訂單狀態 (PENDING/PAID/SHIPPED/COMPLETED/CANCELLED)',
                    FOREIGN KEY (customer_id) REFERENCES customers(id)
                ) COMMENT '訂單主表';
            """))

            # 建立 order_items 表
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS order_items (
                    id INT AUTO_INCREMENT PRIMARY KEY COMMENT '明細唯一ID',
                    order_id INT NOT NULL COMMENT '所屬訂單ID',
                    product_id INT NOT NULL COMMENT '購買商品ID',
                    quantity INT NOT NULL COMMENT '購買數量',
                    unit_price DECIMAL(10, 2) NOT NULL COMMENT '購買時的單價',
                    FOREIGN KEY (order_id) REFERENCES orders(id),
                    FOREIGN KEY (product_id) REFERENCES products(id)
                ) COMMENT '訂單明細表';
            """))
            print("4 張資料表建立成功")

            # 清空舊資料
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 0;"))
            conn.execute(text("TRUNCATE TABLE order_items;"))
            conn.execute(text("TRUNCATE TABLE orders;"))
            conn.execute(text("TRUNCATE TABLE products;"))
            conn.execute(text("TRUNCATE TABLE customers;"))
            conn.execute(text("SET FOREIGN_KEY_CHECKS = 1;"))
            
            # --- 產生假資料 ---
            # 種子必須在產生任何資料「之前」設定，且整段過程不得再呼叫
            # datetime.now() —— 只要有一個非決定性來源，整份資料就不可重現。
            random.seed(DATA_RANDOM_SEED)
            print(f"正在產生假資料...（種子={DATA_RANDOM_SEED}，"
                  f"時間錨點={DATA_ANCHOR_DATE}）")

            cities = ['台北市', '新北市', '桃園市', '台中市', '台南市', '高雄市', '新竹市', '嘉義市']
            first_names = ['小明', '阿華', '美玲', '志強', '淑芬', '俊傑', '雅婷', '家豪', '婉君', '宗憲', '心怡', '建國', '佳穎', '柏翰', '宜蓁']
            last_names = ['陳', '林', '黃', '張', '李', '王', '吳', '劉', '蔡', '楊', '許', '鄭', '謝', '郭', '洪']
            
            # 1. 產生 Customers (50筆)
            customer_data = []
            for i in range(1, 51):
                name = f"{random.choice(last_names)}{random.choice(first_names)}"
                email = f"user{i}@example.com"
                phone = f"09{random.randint(10000000, 99999999)}"
                city = random.choice(cities)
                created_at = DATA_ANCHOR_DATETIME - timedelta(days=random.randint(10, 365))
                customer_data.append({"name": name, "email": email, "phone": phone, "city": city, "created_at": created_at})
            
            conn.execute(text("""
                INSERT INTO customers (name, email, phone, city, created_at) 
                VALUES (:name, :email, :phone, :city, :created_at)
            """), customer_data)

            # 2. 產生 Products (40筆)
            categories = ['3C數位', '家電', '書籍', '生活用品', '服飾', '食品']
            products = {
                '3C數位': [('iPhone 15', 30000), ('MacBook Air', 35000), ('AirPods', 5000), ('iPad Pro', 25000), ('小米手環', 1000), ('行動電源', 800), ('無線滑鼠', 500), ('機械鍵盤', 2000)],
                '家電': [('掃地機器人', 12000), ('空氣清淨機', 8000), ('除濕機', 6000), ('微波爐', 3000), ('電鍋', 2500), ('吹風機', 1500), ('快煮壺', 1000)],
                '書籍': [('原子習慣', 300), ('被討厭的勇氣', 300), ('投資金律', 450), ('刻意練習', 350), ('哈利波特', 400), ('三體', 400), ('金庸群俠傳', 250)],
                '生活用品': [('衛生紙(箱)', 500), ('洗衣精', 200), ('洗髮乳', 150), ('牙膏', 100), ('垃圾袋', 50), ('洗碗精', 80)],
                '服飾': [('純棉T恤', 399), ('牛仔褲', 899), ('運動鞋', 2500), ('羽絨外套', 3500), ('襪子(組)', 150), ('帽子', 450)],
                '食品': [('泡麵(箱)', 400), ('燕麥片', 250), ('咖啡豆', 500), ('礦泉水(箱)', 200), ('洋芋片', 60), ('巧克力', 100)]
            }
            
            product_data = []
            for cat, items in products.items():
                for item_name, price in items:
                    product_data.append({
                        "name": item_name,
                        "category": cat,
                        "price": price,
                        "stock": random.randint(10, 500),
                        "description": f"優質 {item_name}，物超所值"
                    })
            
            conn.execute(text("""
                INSERT INTO products (name, category, price, stock, description) 
                VALUES (:name, :category, :price, :stock, :description)
            """), product_data)

            # 3. 產生 Orders 和 Order_Items (200筆訂單，約500筆明細)
            order_data = []
            order_item_data = []
            
            statuses = ['PENDING', 'PAID', 'SHIPPED', 'COMPLETED', 'CANCELLED']
            
            # 取回產品以獲取ID和價格。
            # ORDER BY 不可省：random.sample 取的是「這個 list 的位置」，
            # MySQL 不保證沒有 ORDER BY 時的回傳順序，順序一變挑中的商品就變了。
            products_db = conn.execute(
                text("SELECT id, price FROM products ORDER BY id")
            ).fetchall()
            
            order_id_counter = 1
            for _ in range(200):
                customer_id = random.randint(1, 50)
                order_date = DATA_ANCHOR_DATETIME - timedelta(days=random.randint(1, 180), hours=random.randint(1, 24))
                status = random.choices(statuses, weights=[10, 10, 20, 50, 10])[0]
                
                # 訂單明細
                num_items = random.randint(1, 5)
                total_amount = 0
                
                # 隨機挑選產品
                chosen_products = random.sample(products_db, num_items)
                for prod in chosen_products:
                    qty = random.randint(1, 3)
                    price = prod[1]
                    total_amount += float(price) * qty
                    
                    order_item_data.append({
                        "order_id": order_id_counter,
                        "product_id": prod[0],
                        "quantity": qty,
                        "unit_price": price
                    })
                
                order_data.append({
                    "id": order_id_counter,
                    "customer_id": customer_id,
                    "order_date": order_date,
                    "total_amount": total_amount,
                    "status": status
                })
                
                order_id_counter += 1

            conn.execute(text("""
                INSERT INTO orders (id, customer_id, order_date, total_amount, status) 
                VALUES (:id, :customer_id, :order_date, :total_amount, :status)
            """), order_data)

            conn.execute(text("""
                INSERT INTO order_items (order_id, product_id, quantity, unit_price) 
                VALUES (:order_id, :product_id, :quantity, :unit_price)
            """), order_item_data)
            
            conn.commit()
            print(f"假資料寫入完成！共 50 位客戶，{len(product_data)} 項商品，200 筆訂單，{len(order_item_data)} 筆訂單明細。")

    except SQLAlchemyError as e:
        print(f"資料庫初始化失敗: {e}")

if __name__ == "__main__":
    init_database()
