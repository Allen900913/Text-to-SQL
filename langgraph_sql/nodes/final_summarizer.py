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
from langgraph_sql.utils.llm_retry import invoke_with_retry


# ===========================================================================
# Summarizer System Prompt
# ===========================================================================

_SCHEMA_UNSUPPORTED_MARKER = "SCHEMA_UNSUPPORTED"

_SCHEMA_UNSUPPORTED_ANSWER = (
    "抱歉，這個問題涉及的欄位或業務概念目前不在資料庫的 schema 與規則定義中，"
    "我無法在不臆測的情況下回答。請確認欄位名稱，或改用資料庫中實際存在的指標重新提問。"
)

_SUMMARIZER_SYSTEM_PROMPT = """你是一個專業的繁體中文資料分析師。
你的任務是根據資料庫查詢結果，以友善且專業的繁體中文回答使用者的問題。

嚴格遵守以下規則：
1. 所有數字必須來自查詢結果，禁止自行計算或臆測。
2. 禁止在回答中顯示任何 SQL 語句。
3. 金額請加上千分位分隔符和「元」（例如：12,000 元）。
4. 如果查詢結果為空 ([])，請明確回答「沒有符合條件的資料」或「查無結果」。
5. 回答要簡潔、直接、有條理。
6. 若結果超過 10 筆，請摘要列出前幾筆並告知總筆數。
7. 使用項目符號或編號列表來呈現多筆資料。
8. 提及總筆數時，「必須」直接引用 Prompt 中提供的「結果總筆數」，絕對禁止自行清點 JSON 陣列的長度。
   你列出的項目數量通常少於總筆數（因為只摘要前幾筆），不可把「列出的數量」當成「總筆數」。"""


# ===========================================================================
# Node 主函數
# ===========================================================================

def final_summarizer(state: AgentState) -> dict:
    """將查詢結果轉為繁體中文自然語言回答。"""
    log.info("[Node 6] Final Summarizer — 生成最終回答")

    champion_sql = state.get("champion_sql", "")
    champion_result = state.get("champion_result", "")
    user_query = state.get("user_query", "")
    error_message = state.get("error_message", "")

    # SQL Generator 觸發了防禦機制（問題涉及 schema 未定義的概念），
    # 直接回覆固定訊息，不把哨兵值交給 LLM 詮釋。
    if _SCHEMA_UNSUPPORTED_MARKER in champion_sql or _SCHEMA_UNSUPPORTED_MARKER in champion_result:
        log.info("[Node 6] 偵測到 SCHEMA_UNSUPPORTED 暗號，略過 LLM 直接回覆")
        return {"final_answer": _SCHEMA_UNSUPPORTED_ANSWER}

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
    # 筆數由 Executor 以 len(df) 算好傳入，不讓 8B 模型自行清點長 JSON（會數錯）。
    row_count = state.get("champion_row_count")
    row_count_line = (
        f"結果總筆數：{row_count} 筆（這是權威數字，回答中提到總數時必須使用它）"
        if row_count is not None
        else ""
    )

    user_prompt = f"""使用者的問題：{user_query}

{row_count_line}

資料庫查詢結果（JSON 格式）：
{champion_result}

請根據以上查詢結果，用繁體中文回答使用者的問題。"""

    raw, llm_error = invoke_with_retry(
        llm_summarizer,
        [SystemMessage(content=_SUMMARIZER_SYSTEM_PROMPT),
         HumanMessage(content=user_prompt)],
        tag="[Node 6] Summarizer",
    )

    if llm_error:
        # SQL 已經跑完、資料是對的，只是自然語言轉換失敗。
        # 直接回傳原始結果，不要讓整題變成沒有答案。
        answer = (
            f"查詢結果已取得，但自然語言轉換失敗。\n"
            f"原始結果：\n{champion_result}"
        )
    else:
        answer = raw.strip()

    log.info("[Node 6] 最終回答生成完成")
    log.debug(f"[Node 6] 回答預覽: {answer[:200]}...")

    return {"final_answer": answer, "llm_error": llm_error}
