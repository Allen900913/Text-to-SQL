"""
LangGraph 圖定義與編排
========================
將 6 個 Node 組裝為 StateGraph，定義條件邊（Conditional Edges）實現：
  - AST 失敗重試 → 回到 SQL Generator
  - Critic 不通過 → 回到 SQL Generator（帶 feedback）
  - 重試次數耗盡 → 錯誤終止或強制輸出

Graph 流程：
  context_retriever → sql_generator → ast_validator
      ↓ (valid)                         ↑ (retry)
  executor_voter → semantic_critic ───────┘
      ↓ (pass)
  final_summarizer → END
"""
from loguru import logger as log
from langgraph.graph import StateGraph, END

from langgraph_sql.state import AgentState
from langgraph_sql.config import MAX_RETRIES

from langgraph_sql.nodes.context_retriever import context_retriever
from langgraph_sql.nodes.sql_generator import sql_generator
from langgraph_sql.nodes.ast_validator import ast_validator
from langgraph_sql.nodes.executor_voter import executor_voter
from langgraph_sql.nodes.semantic_critic import semantic_critic
from langgraph_sql.nodes.final_summarizer import final_summarizer


# ===========================================================================
# 路由函數（Conditional Edge Routing）
# ===========================================================================

def route_after_ast(state: AgentState) -> str:
    """
    AST Validator 之後的路由邏輯：
      - valid_sqls 非空 → 進入 Executor & Voter
      - valid_sqls 為空 且 retry 預算未耗盡 → 重回 SQL Generator
      - valid_sqls 為空 且 retry 預算耗盡 → 錯誤終止
    """
    if state.get("valid_sqls"):
        return "executor_voter"

    retry = state.get("retry_count", 0)
    if retry <= MAX_RETRIES:
        log.info(f"[Router] AST 全部失敗 → 重回 SQL Generator (retry={retry})")
        return "sql_generator"

    log.warning("[Router] AST 全部失敗且重試次數耗盡 → 錯誤終止")
    return "error_end"


def route_after_executor(state: AgentState) -> str:
    """
    Executor & Voter 之後的路由邏輯：
      - 有 champion_sql → 進入 Semantic Critic
      - 全部失敗 → 錯誤終止
    """
    if state.get("champion_sql"):
        return "semantic_critic"

    log.warning("[Router] 所有 SQL 執行失敗 → 錯誤終止")
    return "error_end"


def route_after_critic(state: AgentState) -> str:
    """
    Semantic Critic 之後的路由邏輯：
      - Critic 通過 → 進入 Final Summarizer
      - Critic 不通過 且 retry 預算未耗盡 → 重回 SQL Generator
      - Critic 不通過 且 retry 預算耗盡 → 強制進入 Final Summarizer（帶警告）
    """
    if state.get("critic_passed"):
        return "final_summarizer"

    retry = state.get("retry_count", 0)
    if retry <= MAX_RETRIES:
        log.info(
            f"[Router] Critic 不通過 → 重回 SQL Generator (retry={retry})"
        )
        return "sql_generator"

    log.warning("[Router] Critic 不通過且重試次數耗盡 → 強制輸出（帶警告）")
    return "final_summarizer"


# ===========================================================================
# 錯誤處理 Node
# ===========================================================================

def error_end_node(state: AgentState) -> dict:
    """錯誤終止節點：將 error_message 轉為 final_answer。"""
    error = state.get("error_message", "未知錯誤")
    log.error(f"[Error End] 系統無法完成查詢: {error}")
    return {
        "final_answer": (
            f"抱歉，系統經過多次嘗試仍無法為您的問題生成有效的查詢。\n"
            f"原因：{error}\n"
            f"建議：請嘗試用更明確的方式描述您的問題。"
        )
    }


# ===========================================================================
# 建構 Graph
# ===========================================================================

def build_graph():
    """建構並編譯 LangGraph StateGraph。"""
    log.info("正在建構 LangGraph Pipeline...")

    graph = StateGraph(AgentState)

    # ---- 加入所有 Node ----
    graph.add_node("context_retriever", context_retriever)
    graph.add_node("sql_generator", sql_generator)
    graph.add_node("ast_validator", ast_validator)
    graph.add_node("executor_voter", executor_voter)
    graph.add_node("semantic_critic", semantic_critic)
    graph.add_node("final_summarizer", final_summarizer)
    graph.add_node("error_end", error_end_node)

    # ---- 設定入口 ----
    graph.set_entry_point("context_retriever")

    # ---- 固定邊（無條件流轉） ----
    graph.add_edge("context_retriever", "sql_generator")
    graph.add_edge("sql_generator", "ast_validator")

    # ---- 條件邊（有條件分歧） ----
    graph.add_conditional_edges(
        "ast_validator",
        route_after_ast,
        {
            "executor_voter": "executor_voter",
            "sql_generator": "sql_generator",
            "error_end": "error_end",
        },
    )

    graph.add_conditional_edges(
        "executor_voter",
        route_after_executor,
        {
            "semantic_critic": "semantic_critic",
            "error_end": "error_end",
        },
    )

    graph.add_conditional_edges(
        "semantic_critic",
        route_after_critic,
        {
            "final_summarizer": "final_summarizer",
            "sql_generator": "sql_generator",
        },
    )

    # ---- 終止邊 ----
    graph.add_edge("final_summarizer", END)
    graph.add_edge("error_end", END)

    compiled = graph.compile()
    log.info("LangGraph Pipeline 建構完成 ✅")
    return compiled


# ===========================================================================
# 全域編譯好的 Graph（模組載入時即編譯）
# ===========================================================================
compiled_graph = build_graph()
