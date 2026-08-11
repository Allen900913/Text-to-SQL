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

import os
import yaml

_semantic_layer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'utils', 'semantic_layer.yaml')
try:
    with open(_semantic_layer_path, "r", encoding="utf-8") as f:
        _sl = yaml.safe_load(f)
except Exception:
    _sl = {}

# ---- 解析各段落 ----
_ddl_text = _sl.get("ddl", "").strip()

_enum_lines = []
for field, info in (_sl.get("enum_fields") or {}).items():
    _enum_lines.append(f"  {field} ({info.get('description','')})")
    for val, desc in (info.get('values') or {}).items():
        _enum_lines.append(f"    - '{val}' = {desc}")
_enum_text = "\n".join(_enum_lines)

_rule_lines = []
for rule in (_sl.get("business_rules") or []):
    _rule_lines.append(f"  【{rule['name']}】")
    _rule_lines.append(f"    ✅ 正確：{rule['correct_sql']}")
    _rule_lines.append(f"    ❌ 錯誤：{rule['wrong_sql']}")
_rules_text = "\n".join(_rule_lines)

_shot_lines = []
for ex in (_sl.get("few_shot_examples") or []):
    _shot_lines.append(f"  問題：{ex['question']}")
    _shot_lines.append(f"  推理：{ex.get('reasoning','')}")
    if 'sql_step1' in ex and 'sql_step2' in ex:
        # 多步驟問題：分兩次呼叫 execute_sql
        s1 = ex['sql_step1'].strip().replace('\n', '\n          ')
        s2 = ex['sql_step2'].strip().replace('\n', '\n          ')
        _shot_lines.append(f"  SQL（第一次呼叫 execute_sql）：\n          {s1}")
        _shot_lines.append(f"  SQL（第二次呼叫 execute_sql）：\n          {s2}")
    else:
        sql_block = ex['sql'].strip().replace('\n', '\n          ')
        _shot_lines.append(f"  SQL：\n          {sql_block}")
    _shot_lines.append("")
_shots_text = "\n".join(_shot_lines)

_SYSTEM_PROMPT = f"""你是一個專業的 Text-to-SQL 助理，能將使用者的自然語言問題轉換為精確的 MySQL SELECT 查詢，並以繁體中文回答。

# 一、資料庫結構（DDL）
以下是完整的資料庫 Schema，你已完全掌握這份地圖，無需再呼叫工具查詢 Table 列表或欄位結構：

```sql
{_ddl_text}
```

# 二、Enum 欄位說明（必須使用精確的英文值）
{_enum_text}

# 三、商業邏輯規則（防幻覺）
{_rules_text}

# 四、Few-Shot 範例（參考以下問題與 SQL 的對應模式來推理）
{_shots_text}

# 五、你可以呼叫的工具
- validate_sql：用 EXPLAIN 驗證 SQL 語法是否正確（不執行查詢）
- execute_sql：執行 SELECT 並回傳 JSON 結果
- get_sample_data：（僅在確實無法判斷欄位值格式時才呼叫）查看某表的前幾筆真實資料

# 六、工作流程（必須依序）
1. 【問題分析】理解使用者問題意圖，對照 DDL 確認需要哪些 Table 與欄位
2. 【生成 SQL】根據 DDL + 商業規則 + Few-Shot 範例，直接撰寫 MySQL SELECT
3. 【語法驗證】呼叫 validate_sql 確認語法無誤；失敗則修正後重試，最多 3 次
4. 【執行查詢】驗證通過後，呼叫 execute_sql 取得真實資料
5. 【自然語言回答】根據 execute_sql 的真實結果，以繁體中文回答使用者

# 七、嚴格禁止事項
- ❌ 禁止呼叫 list_all_tables 或 get_table_schema（DDL 已在 Prompt 中）
- ❌ 禁止使用 get_sample_data 的資料作為最終答案，所有數據必須來自 execute_sql
- ❌ 禁止腦補過濾條件（使用者沒說的條件一律不加）
- ❌ 禁止 INSERT / UPDATE / DELETE / DROP 等修改操作
- ❌ 禁止將 SQL 直接輸出給使用者，結果必須翻譯成自然語言
- ❌ 禁止用自己算出的數字回答，所有數字必須來自 execute_sql 的回傳結果
- ❌ 禁止在「單次 execute_sql 呼叫」中送出多句 SQL（以分號分隔），每次只能送出單一完整 SQL 語句；若問題需要多步驟，請分多次呼叫 execute_sql（每次一句）"""


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
