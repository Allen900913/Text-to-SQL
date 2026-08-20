import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from langgraph_sql.graph import compiled_graph

def run_unseen_tests():
    # 這些是完全沒有出現在 few-shot 範例中的「全新」問題
    questions = [
        "1. 請問『筆記型電腦』(Laptop)這個類別底下，總共有幾種不同的商品？",
        "2. 幫我列出單價最貴的前三名商品名稱和價格。",
        "3. 在 2026 年 5 月份的所有訂單中，總共創造了多少營業額？",
        "4. 找出購買商品數量總和最多的那位客戶，並列出他的電子郵件。",
        "5. 請找出狀態是『已出貨』(SHIPPED) 的訂單中，平均每筆訂單的總金額大約是多少？"
    ]
    
    with open("test_generalization_results.txt", "w", encoding="utf-8") as f:
        for i, q in enumerate(questions):
            print(f"\n[{i+1}/5] 測試未見過的題目: {q}")
            f.write(f"問題: {q}\n")
            
            actual_q = q.split('. ', 1)[1]
            
            try:
                start_time = time.time()
                result = compiled_graph.invoke({
                    "user_query": actual_q,
                    "retry_count": 0
                })
                elapsed = time.time() - start_time
                
                print(f"  -> 完成 ({elapsed:.1f}s)")
                print(f"  -> SQL: {result.get('champion_sql')}")
                f.write(f"回答: {result.get('final_answer')}\n")
                f.write(f"SQL: {result.get('champion_sql')}\n")
                f.write(f"EXPLAIN 通過: {result.get('sql_validated')} | 重試次數: {result.get('retry_count')}\n")
                f.write("-" * 50 + "\n")
            except Exception as e:
                print(f"  -> 失敗: {e}")
                f.write(f"錯誤: {e}\n")
                f.write("-" * 50 + "\n")
                
    print("\n所有未見過的題目測試完成，結果已存入 test_generalization_results.txt")

if __name__ == "__main__":
    run_unseen_tests()
