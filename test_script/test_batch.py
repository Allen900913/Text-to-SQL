import time
from agent import TextToSQLAgent

def run_tests():
    agent = TextToSQLAgent()
    
    questions = [
        "1. 找出所有姓「陳」的客戶名單。",
        "2. 總共賣出幾台「空氣清淨機」？",
        "3. 在台北市的客戶，總共下了多少張訂單？",
        "4. 哪一個商品的總營收最高？總共賺了多少錢？",
        "5. 有多少張訂單的狀態是待處理 (PENDING)？",
        "6. 找出從未下過任何訂單的客戶姓名。",
        "7. 單筆訂單總金額超過 5 萬元的訂單，都是哪些客戶買的？",
        "8. 最晚註冊的客戶是誰？他註冊的日期是哪天？",
        "9. 商品庫存量低於 10 的商品有哪些？",
        "10. 平均每張訂單買了幾項不同的商品？"
    ]
    
    with open("test_batch_results.txt", "w", encoding="utf-8") as f:
        for i, q in enumerate(questions):
            print(f"[{i+1}/10] 測試中: {q}")
            f.write(f"問題: {q}\n")
            
            try:
                start_time = time.time()
                answer = agent.run(q.split('. ', 1)[1])
                elapsed = time.time() - start_time
                
                print(f"  -> 完成 ({elapsed:.1f}s)")
                f.write(f"回答: {answer}\n")
                f.write("-" * 50 + "\n")
            except Exception as e:
                print(f"  -> 失敗: {e}")
                f.write(f"錯誤: {e}\n")
                f.write("-" * 50 + "\n")
                
    print("所有測試完成，結果已存入 test_batch_results.txt")

if __name__ == "__main__":
    run_tests()
