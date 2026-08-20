"""
LangGraph Funnel Pipeline — 狀態定義
======================================
定義 AgentState TypedDict，所有 Node 透過讀寫此 State 溝通。
"""
from typing import TypedDict


class AgentState(TypedDict, total=False):
    """LangGraph 圖的共用狀態。"""

    # --- 輸入 ---
    user_query: str                # 使用者原始問題

    # --- Node 1: Context Retriever 填入 ---
    schema_ddl: str                # DDL Schema 文字（已依檢索結果剪裁）
    enum_text: str                 # Enum 欄位說明
    retrieved_tables: list[str]    # 檢索層選出、實際進 Prompt 的表
    retrieval_anchors: list[str]   # 語意檢索命中的錨點（KMB 補橋接表之前）
    rules_text: str                # 商業邏輯規則
    few_shot_examples: str         # Few-Shot 範例

    # --- Node 2: SQL Generator 填入 ---
    candidate_sqls: list[str]      # N 條候選 SQL

    # --- Node 3: AST Validator 填入 ---
    valid_sqls: list[str]          # 通過快篩的 SQL

    # --- Node 4/5: DB 驗證與執行階段填入 ---
    execution_results: dict        # 執行結果暫存
    champion_sql: str              # 投票勝出的 SQL
    champion_result: str           # 勝出 SQL 的執行結果 (JSON)
    champion_row_count: int        # 結果筆數（權威值，避免 Summarizer 自行清點 JSON 出錯）

    # --- SQL 驗證 / 修復迴圈 ---
    db_error: str                  # AST、EXPLAIN 或執行階段的錯誤訊息
    sql_validated: bool            # 是否已通過 MySQL EXPLAIN

    # --- 控制流 ---
    retry_count: int               # 重試計數器（總預算，跨 Node 共用）

    # --- 基礎設施錯誤 ---
    # 與 error_message / db_error 刻意分開：這兩者代表「模型寫錯 SQL」，
    # llm_error 代表「LLM API 根本沒回應」（逾時、429、503）。評估時若混為一談，
    # 會把網路問題誤記成模型能力不足。
    llm_error: str

    # --- 輸出 ---
    final_answer: str              # 最終自然語言回答
    error_message: str             # 錯誤訊息（用於 fallback）
