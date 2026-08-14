"""
LangGraph Funnel Pipeline — CLI 互動進入點
============================================
執行方式：
  cd d:\\text_to_sql
  python -m langgraph_sql.main
"""
import sys
import os

# 確保能找到上層模組（當直接以 python langgraph_sql/main.py 執行時）
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from langgraph_sql.graph import compiled_graph
from langgraph_sql.config import log


def _safe_str(text: str) -> str:
    """移除無法編碼的字元，防止 Windows console 崩潰。"""
    return text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def main():
    sep = "=" * 60

    print(sep)
    print("  🧠 LangGraph Funnel Pipeline — Text-to-SQL 智慧助理")
    print(sep)
    print("  架構：SQL 生成 → AST 安全快篩 → MySQL EXPLAIN → 查詢執行")
    print("  輸入自然語言問題，系統會自動完成整條 Pipeline 並回答。")
    print("  輸入 'exit' 或 'quit' 退出。")
    print("  請確認已執行 python init_db.py 完成資料庫初始化！")
    print(sep)

    while True:
        try:
            user_input = input("\n  請問您想查詢什麼資料？\n> ").strip()

            if user_input.lower() in ("exit", "quit", "q"):
                print("\n  感謝使用，下次見！👋")
                break

            if not user_input:
                print("  請輸入問題。")
                continue

            print("\n  ⏳ Funnel Pipeline 處理中...\n")
            print("-" * 60)

            # 執行 LangGraph Pipeline
            result = compiled_graph.invoke({
                "user_query": user_input,
                "retry_count": 0,
            })

            print("-" * 60)

            answer = result.get("final_answer", "無法取得回答")
            print(f"\n  📋 回答：\n{_safe_str(answer)}\n")
            print(sep)

        except KeyboardInterrupt:
            print("\n\n  已中斷，感謝使用！")
            break
        except Exception as e:
            log.exception(e)
            print(f"\n  ❌ 發生未預期的錯誤: {e}")


if __name__ == "__main__":
    main()
