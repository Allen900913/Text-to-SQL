"""
Text-to-SQL Agent
=================
使用 LangGraph (langgraph.prebuilt.create_react_agent) + 5 個專用 Tools，
實作圖片中的完整工作流程：

  用戶輸入自然語言
        ↓
  問題重寫與關鍵詞提取  (LLM Thought / Tool Calling)
        ↓
  Schema 回憶與表關係解析  (list_all_tables + get_table_schema)
        ↓
  (必要時查看範例資料)  (get_sample_data)
        ↓
  SQL 查詢生成  (LLM 推理)
        ↓
  SQL 語法驗證  (validate_sql → EXPLAIN)
        ↓
  語義一致性校驗  (LLM 自省)
        ↓
  校驗是否通過？ → 否 → 回到 SQL 生成（LangGraph ReAct 自動重試）
        ↓ 是
  執行查詢  (execute_sql)
        ↓
  結果轉換為自然語言  (Final Answer)
"""
from langchain.agents import create_agent
from langchain_core.tools import tool
from langchain_core.messages import HumanMessage, AIMessage

from config import llm, MYSQL_URI
from utils.db_utils import MySQLDatabaseManager
from utils.logger import log


def _safe_str(text: str) -> str:
    """移除 surrogate pair 等無法被 cp950 或 utf-8 編碼的字元，防止 Windows console 崩潰。"""
    return text.encode('utf-8', errors='replace').decode('utf-8', errors='replace')


import threading

# ==============================================================================
# 1. 共用資料庫連線（惰性初始化，避免 import 時就連線）
# ==============================================================================

_db_manager: MySQLDatabaseManager | None = None
_db_lock = threading.Lock()


def _get_db() -> MySQLDatabaseManager:
    """取得（或建立）全域資料庫管理器實例。使用 Double-checked locking 防止多執行緒重複初始化。"""
    global _db_manager
    if _db_manager is None:
        with _db_lock:
            if _db_manager is None:
                _db_manager = MySQLDatabaseManager(MYSQL_URI)
                log.info("資料庫連線初始化完成")
    return _db_manager


# ==============================================================================
# 2. Tools 定義（5 個工具，供 Agent 透過 Tool Calling 自主呼叫）
# ==============================================================================

@tool
def list_all_tables(dummy: str = "") -> str:
    """
    列出資料庫中「所有」資料表的名稱與中文說明。
    這是分析使用者問題的第一步，必須先呼叫此工具，才能知道資料庫中有哪些表可以查詢。
    此工具不需要任何輸入，傳入空字串即可。
    """
    try:
        db = _get_db()
        tables = db.get_tables_with_comments()
        if not tables:
            return "資料庫中沒有任何資料表。"
        lines = ["目前資料庫中共有以下資料表：\n"]
        for t in tables:
            comment = t.get("table_comment") or "（無說明）"
            lines.append(f"  - {t['table_name']}: {comment}")
        return "\n".join(lines)
    except Exception as e:
        log.error(f"list_all_tables 失敗: {e}")
        return f"查詢資料表清單失敗: {str(e)}"


@tool
def get_table_schema(table_names: str) -> str:
    """
    取得指定資料表的完整欄位結構，包含：欄位名稱、資料型態、是否為主鍵、外鍵關係與索引資訊。
    在生成 SQL 之前，必須先查詢所有相關資料表的 Schema，以確保欄位名稱和型態正確。
    輸入格式：以逗號分隔的資料表名稱字串。
    範例輸入：customers
    範例輸入：orders,order_items,products
    """
    try:
        db = _get_db()
        names = [n.strip() for n in table_names.split(",") if n.strip()]
        if not names:
            return "錯誤：請提供至少一個資料表名稱。"
        return db.get_table_schema(names)
    except Exception as e:
        log.error(f"get_table_schema 失敗: {e}")
        return f"查詢 Schema 失敗: {str(e)}"


@tool
def get_sample_data(table_name: str) -> str:
    """
    取得指定資料表的前 1 筆範例資料（JSON 格式）。
    當你需要了解某個欄位的實際值格式（例如日期格式、status 欄位的可能值、數值範圍）時，
    應呼叫此工具確認，以確保生成的 SQL 條件正確。
    請節省使用，只有在必要時才呼叫。
    輸入格式：單一資料表名稱字串。
    範例輸入：orders
    範例輸入：customers
    """
    try:
        db = _get_db()
        result = db.get_sample_data(table_name.strip(), limit=1)
        return f"資料表 [{table_name}] 的前 1 筆範例資料：\n{result}"
    except Exception as e:
        log.error(f"get_sample_data 失敗: {e}")
        return f"查詢範例資料失敗: {str(e)}"


@tool
def validate_sql(sql: str) -> str:
    """
    使用 MySQL 的 EXPLAIN 指令驗證 SQL 語法是否正確。
    此工具不會實際執行查詢，也不會修改任何資料，純粹用於語法驗證。
    生成 SQL 之後、執行查詢之前，「必須」先呼叫此工具進行驗證。
    若驗證失敗，請根據錯誤訊息修正 SQL 後重新驗證，最多嘗試 3 次。
    輸入格式：完整的 MySQL SELECT 語句（不需要加上 markdown 格式）。
    範例輸入：SELECT * FROM customers LIMIT 5
    """
    try:
        db = _get_db()
        sql_clean = sql.strip().replace("```sql", "").replace("```", "").strip()
        result = db.validate_sql_syntax(sql_clean)
        return result
    except Exception as e:
        log.warning(f"validate_sql 失敗: {e}")
        return f"SQL 語法驗證失敗，請修正後重試。錯誤：{str(e)}"


@tool
def execute_sql(sql: str) -> str:
    """
    執行 SELECT SQL 查詢並回傳結果（JSON 格式，最多 100 筆）。
    注意：只有在 validate_sql 確認語法正確後，才應呼叫此工具。
    只允許 SELECT 或 WITH 開頭的唯讀查詢，任何修改操作都會被拒絕。
    輸入格式：完整的 MySQL SELECT 語句（不需要加上 markdown 格式）。
    範例輸入：SELECT c.name, COUNT(o.id) AS order_count FROM customers c JOIN orders o ON c.id = o.customer_id GROUP BY c.id ORDER BY order_count DESC
    """
    try:
        db = _get_db()
        sql_clean = sql.strip().replace("```sql", "").replace("```", "").strip()
        result = db.execute_query(sql_clean)
        return result
    except Exception as e:
        log.error(f"execute_sql 失敗: {e}")
        return f"SQL 執行失敗: {str(e)}"


# ==============================================================================
# 3. System Prompt（對應圖片的工作流程，使用 Tool Calling 模式）
# ==============================================================================

_SYSTEM_PROMPT = """你是一個專業的資料庫查詢助理（Text-to-SQL Agent），能夠將使用者的自然語言問題轉換為精確的 MySQL SELECT 查詢，並以清晰的繁體中文回答。

你擁有以下工具可以呼叫：
- list_all_tables：列出資料庫所有表名與說明
- get_table_schema：查詢指定表的欄位結構、主鍵、外鍵
- get_sample_data：查詢指定表的前 3 筆範例資料（了解資料格式）
- validate_sql：使用 EXPLAIN 驗證 SQL 語法正確性（不執行）
- execute_sql：執行 SELECT 查詢並回傳 JSON 結果

工作流程（必須依序完成）：
1. 【問題分析】仔細分析使用者問題，釐清查詢意圖和需要哪些資料
2. 【探索資料庫】呼叫 list_all_tables，了解有哪些可用的表
3. 【Schema 分析】呼叫 get_table_schema，查詢所有相關表的欄位結構與外鍵關係
4. 【資料格式確認】（選填）若不確定某欄位的值格式，呼叫 get_sample_data 確認
5. 【生成 SQL】根據 Schema 撰寫正確的 MySQL SELECT 語句
6. 【語法驗證】呼叫 validate_sql 確認 SQL 語法無誤
7. 【語義校驗】自我審查：這個 SQL 能精確回答使用者問題嗎？
8. 【執行查詢】確認無誤後，呼叫 execute_sql 執行查詢
9. 【自然語言回答】整理結果，用清晰友善的繁體中文回答使用者

重要規則：
- 只能執行 SELECT 查詢，嚴禁任何修改操作（INSERT/UPDATE/DELETE/DROP 等）。
- 嚴禁直接將 SQL 語法當作答案輸出給使用者！你必須透過「呼叫工具 (Tool Calling)」來執行 `validate_sql` 與 `execute_sql`，並在拿到資料後，再統整成自然語言回答使用者。
- 必須先 validate_sql 驗證，再 execute_sql 執行。若 validate_sql 失敗，修正 SQL 並重新驗證，最多嘗試 3 次。
- 注意：一次思考步驟中，同一個工具只需要呼叫一次，請勿重複送出相同的工具呼叫。
- 為了避免超出 Token 限制，探索 Schema 與 Sample data 時請「精準挑選真正相關的表」，不要一次查詢所有表，且只有在絕對必要時才查詢 Sample data。
- ⚠️ 嚴禁在同一個思考步驟中「同時呼叫」查詢 Schema（或 Sample Data）與執行 SQL（validate/execute）的工具。你必須先查 Schema，等待並閱讀結果後，在「下一個步驟」才能開始寫 SQL。
- ⚠️ 絕對不可以拿 `get_sample_data` 回傳的範例資料來回答使用者的問題。所有最終回答的數據都必須來自 `execute_sql` 的真實回傳結果。
- 最終回答使用繁體中文，直接呈現查詢結果，不要向使用者提及任何 SQL 語法或程式碼細節。"""


# ==============================================================================
# 4. 建立 Agent（改用 langchain.agents.create_agent）
# ==============================================================================

_ALL_TOOLS = [list_all_tables, get_table_schema, get_sample_data, validate_sql, execute_sql]

_agent_graph = create_agent(
    model=llm,
    tools=_ALL_TOOLS,
    system_prompt=_SYSTEM_PROMPT,  # 注意參數名稱為 system_prompt
)


# ==============================================================================
# 5. 對外統一入口
# ==============================================================================

class TextToSQLAgent:
    """Text-to-SQL Agent 的公開入口類別，封裝 LangGraph CompiledStateGraph。"""

    def run(self, user_question: str) -> str:
        """
        接收使用者的自然語言問題，透過 LangGraph ReAct Agent 自主選擇工具完成查詢，
        最終回傳繁體中文的自然語言回答。

        Args:
            user_question: 使用者輸入的自然語言問題

        Returns:
            str: 以繁體中文呈現的查詢結果
        """
        log.info(_safe_str(f"[TextToSQLAgent] 開始處理問題: {user_question}"))
        try:
            result = _agent_graph.invoke(
                {"messages": [HumanMessage(content=user_question)]}
            )
            # result["messages"] 是完整的訊息歷史，最後一條是 AI 的最終回答
            messages = result.get("messages", [])
            if not messages:
                return "抱歉，Agent 沒有回傳任何結果，請嘗試換個方式詢問。"

            # 將中間的工具呼叫與結果寫入 Log (DEBUG 級別)，不在終端機洗版，但會完整保存在檔案裡
            for m in messages:
                # 判斷是否為呼叫工具的訊息 (AIMessage)
                if hasattr(m, 'tool_calls') and m.tool_calls:
                    for tc in m.tool_calls:
                        log.debug(f"🛠️ Agent 呼叫工具: {tc['name']}, 參數: {tc['args']}")
                # 判斷是否為工具回傳的結果 (ToolMessage)
                elif getattr(m, 'type', '') == "tool":
                    content_full = _safe_str(str(m.content)).strip()
                    log.debug(f"✅ 工具 [{m.name}] 完整回傳結果:\n{content_full}\n{'-'*50}")

            # 取最後一條 AIMessage 作為最終回答
            final_msg = messages[-1]
            answer = final_msg.content if hasattr(final_msg, "content") else str(final_msg)
            answer = _safe_str(str(answer))  # 清理 surrogate pair

            log.info("[TextToSQLAgent] 問題處理完成")
            return answer

        except Exception as e:
            import traceback
            err_msg = _safe_str(str(e))
            tb_str = _safe_str(traceback.format_exc())
            log.error(f"[TextToSQLAgent] Agent 執行失敗: {err_msg}\n{tb_str}")
            return f"處理問題時發生錯誤，請稍後再試。（錯誤：{err_msg}）"
