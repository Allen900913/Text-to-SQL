"""
Text-to-SQL 智慧助理 - 命令列互動介面
執行方式：在 text_to_sql/ 目錄下執行 python main.py
"""
import sys
import os

# 確保在任何工作目錄下都能找到同層的模組（agent, config, utils）
_this_dir = os.path.dirname(os.path.abspath(__file__))
if _this_dir not in sys.path:
    sys.path.insert(0, _this_dir)

from agent import TextToSQLAgent
from utils.logger import log


def main():
    sep = "=" * 60
    print(sep)
    print(" Text-to-SQL 智慧助理")
    print(sep)
    print(" 輸入自然語言問題，Agent 會自動查詢資料庫並回答。")
    print(" 輸入 'exit' 或 'quit' 退出。")
    print(" 請確認已執行 python init_db.py 完成資料庫初始化！")
    print(sep)

    try:
        agent = TextToSQLAgent()
        print("\n [OK] 助理初始化成功，已連線至資料庫！\n")
    except Exception as e:
        print(f"\n [ERROR] 助理初始化失敗: {e}")
        print(" 請確認：MySQL 服務是否已啟動？連線字串是否正確？")
        return

    while True:
        try:
            user_input = input("\n 請問您想查詢什麼資料？\n> ").strip()

            if user_input.lower() in ["exit", "quit", "q"]:
                print("\n 感謝使用 Text-to-SQL 助理，下次見！")
                break

            if not user_input:
                print(" 請輸入問題。")
                continue

            print("\n 處理中，Agent 正在思考...\n")
            print("-" * 60)

            answer = agent.run(user_input)

            print("-" * 60)
            print(f"\n 回答：\n{answer}\n")
            print("=" * 60)

        except KeyboardInterrupt:
            print("\n\n 已中斷，感謝使用！")
            break
        except Exception as e:
            print(f"\n [ERROR] 發生未預期的錯誤: {e}")


if __name__ == "__main__":
    main()
