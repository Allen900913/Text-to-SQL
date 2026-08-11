"""
Node 6: Final Summarizer
==========================
使用 8B 模型 (Temperature=0) 將查詢結果轉為友善的繁體中文回答。
此 Node 不需要 70B 模型 — 純粹是格式轉換任務，好鋼用在刀刃上。
"""
from loguru import logger as log
from langchain_core.messages import SystemMessage, HumanMessage

from langgraph_sql.state import AgentState
from langgraph_sql.config import llm_summarizer


# ===========================================================================
# Summarizer System Prompt
# ===========================================================================

_SUMMARIZER_SYSTEM_PROMPT = """你是一個專業的繁體中文資料分析師。
你的任務是根據資料庫查詢結果，以友善且專業的繁體中文回答使用者的問題。

嚴格遵守以下規則：
1. 所有數字必須來自查詢結果，禁止自行計算或臆測。
2. 禁止在回答中顯示任何 SQL 語句。
3. 金額請加上千分位分隔符和「元」（例如：12,000 元）。
4. 如果查詢結果為空 ([])，請明確回答「沒有符合條件的資料」或「查無結果」。
5. 回答要簡潔、直接、有條理。
6. 若結果超過 10 筆，請摘要列出前幾筆並告知總筆數。
7. 使用項目符號或編號列表來呈現多筆資料。"""


# ===========================================================================
# Node 主函數
# ===========================================================================

def final_summarizer(state: AgentState) -> dict:
    """將查詢結果轉為繁體中文自然語言回答。"""
    log.info("[Node 6] Final Summarizer — 生成最終回答")

    champion_result = state.get("champion_result", "")
    user_query = state.get("user_query", "")
    error_message = state.get("error_message", "")

    # 如果有錯誤訊息且無查詢結果，直接回報錯誤
    if error_message and not champion_result:
        return {
            "final_answer": f"抱歉，查詢過程中發生問題：{error_message}"
        }

    if not champion_result:
        return {
            "final_answer": "抱歉，無法取得查詢結果，請嘗試換個方式詢問。"
        }

    # 建構 Prompt
    user_prompt = f"""使用者的問題：{user_query}

資料庫查詢結果（JSON 格式）：
{champion_result}

請根據以上查詢結果，用繁體中文回答使用者的問題。"""

    try:
        response = llm_summarizer.invoke([
            SystemMessage(content=_SUMMARIZER_SYSTEM_PROMPT),
            HumanMessage(content=user_prompt),
        ])
        answer = response.content if hasattr(response, "content") else str(response)
        answer = answer.strip()
    except Exception as e:
        log.error(f"[Node 6] LLM 呼叫失敗: {e}")
        answer = (
            f"查詢結果已取得，但自然語言轉換失敗。\n"
            f"原始結果：\n{champion_result}"
        )

    # 如果 Critic 重試次數已耗盡但仍強制輸出，加上警告
    critic_passed = state.get("critic_passed", True)
    retry_count = state.get("retry_count", 0)
    if not critic_passed and retry_count > 0:
        answer += (
            "\n\n⚠️ 注意：此查詢結果可能不完全符合您的問題意圖，"
            "建議您確認後再使用。"
        )

    log.info("[Node 6] 最終回答生成完成")
    log.debug(f"[Node 6] 回答預覽: {answer[:200]}...")

    return {"final_answer": answer}
