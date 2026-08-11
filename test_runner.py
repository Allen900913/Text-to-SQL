import sys
import os
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from langgraph_sql.graph import compiled_graph

def run_evaluation(file_path: str = "eval_questions.json"):
    if not os.path.exists(file_path):
        print(f"找不到測試檔案 {file_path}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        questions_data = json.load(f)

    total_q = len(questions_data)
    print(f"載入 {total_q} 題測試題目，準備開始批次測試...")
    
    results = []
    success_count = 0

    # 建立報告檔案
    report_file = f"eval_report_{int(time.time())}.txt"
    with open(report_file, "w", encoding="utf-8") as report:
        report.write(f"=== Text-to-SQL 評估報告 ===\n")
        report.write(f"總題數: {total_q}\n\n")

        for i, item in enumerate(questions_data):
            q_id = item.get("id", i+1)
            q_type = item.get("type", "unknown")
            question = item.get("question", "")

            print(f"\n[{i+1}/{total_q}] 類型: {q_type} | 題目: {question}")
            
            try:
                start_time = time.time()
                # 執行 Pipeline
                result = compiled_graph.invoke({
                    "user_query": question,
                    "retry_count": 0
                })
                elapsed = time.time() - start_time
                
                # 記錄結果
                champion_sql = result.get('champion_sql', '')
                final_answer = result.get('final_answer', '')
                critic_passed = result.get('critic_passed', False)
                retries = result.get('retry_count', 0)
                
                # 簡單判定是否成功 (有產出 SQL 且沒有報錯)
                is_success = bool(champion_sql) and ("抱歉" not in final_answer or "錯誤" not in final_answer)
                if is_success:
                    success_count += 1
                
                print(f"  -> 耗時: {elapsed:.1f}s | Critic: {critic_passed} | Retry: {retries}")
                print(f"  -> SQL: {champion_sql[:80]}..." if champion_sql else "  -> 無法生成 SQL")
                
                report.write(f"【第 {q_id} 題】 ({q_type})\n")
                report.write(f"問題: {question}\n")
                report.write(f"回答: {final_answer}\n")
                report.write(f"SQL: {champion_sql}\n")
                report.write(f"Critic 通過: {critic_passed} | 重試次數: {retries} | 耗時: {elapsed:.1f}s\n")
                report.write("-" * 60 + "\n")

            except Exception as e:
                print(f"  -> 執行崩潰: {e}")
                report.write(f"【第 {q_id} 題】 ({q_type})\n問題: {question}\n執行崩潰: {e}\n")
                report.write("-" * 60 + "\n")

    print(f"\n=== 評估完成 ===")
    print(f"成功率: {success_count}/{total_q} ({(success_count/total_q)*100:.1f}%)")
    print(f"詳細報告已儲存至: {report_file}")

if __name__ == "__main__":
    run_evaluation("eval_questions.json")
