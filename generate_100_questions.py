import json
import random

def generate_questions():
    questions = []
    
    # --- 1. 生成 20 題 few-shot 類型的題目 ---
    few_shot_templates = [
        "找出所有姓「{name}」的客戶名單",
        "總共賣出幾台「{product}」？",
        "在{city}的客戶，總共下了多少張訂單？",
        "哪一個商品的總營收最高？總共賺了多少錢？",
        "有多少張訂單的狀態是{status}？",
        "找出從未下過任何訂單的客戶姓名",
        "單筆訂單總金額超過 {amount} 元的訂單，都是哪些客戶買的？",
        "最晚註冊的客戶是誰？他註冊的日期是哪天？",
        "商品庫存量低於 {stock} 的商品有哪些？",
        "平均每張訂單買了幾項不同的商品？"
    ]
    
    names = ["陳", "林", "黃", "張", "李", "王", "吳", "劉"]
    products = ["空氣清淨機", "MacBook Air", "iPhone 15", "iPad Pro", "無線耳機", "咖啡機"]
    cities = ["台北市", "台中市", "高雄市", "台南市", "桃園市", "新竹市"]
    statuses = ["PENDING", "SHIPPED", "DELIVERED", "CANCELLED"]
    amounts = [10000, 20000, 30000, 50000, 80000, 100000]
    stocks = [5, 10, 20, 50]
    
    for i in range(20):
        t = few_shot_templates[i % len(few_shot_templates)]
        q = t.format(
            name=random.choice(names),
            product=random.choice(products),
            city=random.choice(cities),
            status=random.choice(statuses),
            amount=random.choice(amounts),
            stock=random.choice(stocks)
        )
        questions.append({"id": len(questions) + 1, "type": "few_shot", "question": q})

    # --- 2. 生成 80 題非 few-shot 類型的題目 (泛化測試) ---
    unseen_templates = [
        "列出 {year} 年 {month} 月營收最高的前 {n} 名客戶。",
        "購買過「{product}」的客戶，平均每個人總共花了多少錢在我們店裡？",
        "哪一個城市賣出的「{category}」數量最多？總共賣出多少？",
        "請列出所有單次購買過大於 {qty} 件商品的客戶電子郵件。",
        "{status} 狀態的訂單中，哪一天的訂單數量最多？",
        "請找出那些每一張訂單金額都超過 {amount} 元的 VIP 客戶名單。",
        "從來沒有買過「{product}」的客戶有誰？",
        "總庫存量價值（庫存數量乘上單價）最高的三種商品類別是什麼？",
        "單價超過 {price} 元的商品中，哪一個最受歡迎（也就是賣出最多件）？",
        "平均每位客戶註冊後，下了幾張訂單？",
        "找出在 {year} 年註冊，並且至少下過一筆 {status} 訂單的客戶名字。",
        "幫我列出單價最貴的前 {n} 名商品名稱和價格。",
        "請問『{category}』這個類別底下，總共有幾種不同的商品？",
        "找出購買商品數量總和最多的那位客戶，並列出他的電子郵件。",
        "在 {year} 年的所有訂單中，平均每筆訂單的總金額大約是多少？",
        "找出所有沒有填寫信箱的客戶。",
        "列出所有買過『{category}』類別商品，且目前住在『{city}』的客戶。",
        "找出所有『{category}』商品中，單筆訂單購買數量曾經超過 {qty} 的紀錄。",
        "統計各個城市的客戶數量，並依照數量由大到小排序。",
        "請列出在 {year} 年 {month} 月份，單日營業額最高的那一天。"
    ]

    categories = ["Laptop", "Smartphone", "Tablet", "Accessories", "Home Appliances"]
    years = [2023, 2024, 2025, 2026]
    months = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]
    ns = [3, 5, 10]
    qtys = [2, 3, 5, 10]
    prices = [5000, 10000, 20000, 30000]

    for i in range(80):
        t = unseen_templates[i % len(unseen_templates)]
        q = t.format(
            year=random.choice(years),
            month=random.choice(months),
            n=random.choice(ns),
            product=random.choice(products),
            category=random.choice(categories),
            qty=random.choice(qtys),
            status=random.choice(statuses),
            amount=random.choice(amounts),
            price=random.choice(prices),
            city=random.choice(cities)
        )
        questions.append({"id": len(questions) + 1, "type": "unseen", "question": q})

    # 輸出到 JSON
    with open("eval_questions.json", "w", encoding="utf-8") as f:
        json.dump(questions, f, ensure_ascii=False, indent=4)
        
    print(f"成功生成了 {len(questions)} 題測試題並儲存至 eval_questions.json")

if __name__ == "__main__":
    generate_questions()
