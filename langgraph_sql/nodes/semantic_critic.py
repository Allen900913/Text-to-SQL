"""
Node 5: Semantic Critic
========================
使用 70B 模型進行語意決審。

將 champion_sql + user_query 送入 LLM，判斷 SQL 語意是否正確回答了使用者的問題。
若不通過，將退回原因寫入 critic_feedback 並遞增 retry_count，路由會重回 Node 2 重新生成。
"""
import json
import re
from loguru import logger as log
from langchain_core.messages import SystemMessage, HumanMessage

from langgraph_sql.state import AgentState
from langgraph_sql.config import llm_strong
from langgraph_sql.utils.schema_parser import SchemaParser

# 預載 Business Rules（只載入一次）
_schema_parser = SchemaParser()
_BUSINESS_RULES_TEXT = _schema_parser.get_rules_text()


# ===========================================================================
# Critic System Prompt
# ===========================================================================

_CRITIC_SYSTEM_PROMPT = """你是一位嚴格的商業資料稽核員。
你的任務是比對「使用者的原始需求」與「資料庫實際將執行的 SQL 查詢邏輯」，確保兩者語意與過濾條件完全對齊。

請嚴格檢查以下常見錯誤：
1. WHERE 條件是否遺漏（時間、地區、狀態等限制條件）。
2. 聚合函數 (SUM, AVG, COUNT, COUNT DISTINCT) 是否誤用。
3. 業務定義是否不符（例如需求問「銷售數量」，SQL 卻用了 COUNT 而非 SUM(quantity)）。
4. JOIN 邏輯是否正確（是否遺漏必要的 JOIN 或多加了不需要的 JOIN）。
5. 排序與限制是否正確（例如需求問「最高的」，SQL 是否有 ORDER BY DESC LIMIT 1）。
6. 欄位選擇是否完整（使用者問了哪些資訊，SQL 是否都有 SELECT 到）。

## 特別注意清單（Special Attention List）
這些是最容易踱入的陷阱，請對每次都倒追確認：

A. 「時間方向」檢查：
   - 問題中的「以內 / 不到 / 最近」→ SQL 必須用 >= ，不可用 <=。
   - 問題中的「超過 N 天前 / 早於 / 以前」→ SQL 必須用 < ，不可用 >。
   - DATE_SUB 的 INTERVAL 大小與單位是否和問題一致？

B. 「量詞語意」檢查：
   - 問題含「每一張 / 全部 / 所有」→ SQL 不能只用 WHERE，必須用 GROUP BY + HAVING MIN/MAX 做全稱檢驗。
   - 問題含「至少 / 曾經 / 有過」→ SQL 只需 JOIN + WHERE 或 EXISTS，不需全稱結構。

## 本專案業務規則（請對照檢查 SQL）
{business_rules}

請務必輸出以下 JSON 格式，不要附加任何其他文字或 markdown 區塊：
{{"is_match": true, "reason": "Match"}}
或
{{"is_match": false, "reason": "具體指出哪裡不符，例如：遙漏了 status = 'COMPLETED' 的過濾條件"}}"""


def _build_critic_system_prompt() -> str:
    """動態注入業務規則到 Critic Prompt。"""
    return _CRITIC_SYSTEM_PROMPT.format(business_rules=_BUSINESS_RULES_TEXT or "（無額外業務規則）")


# ===========================================================================
# 輔助函數
# ===========================================================================

def _parse_critic_response(raw_text: str) -> tuple[bool, str]:
    """
    解析 Critic 的 JSON 回應。
    若解析失敗，預設為通過（fail-open，避免卡在迴圈中）。
    """
    text = raw_text.strip()

    # 嘗試從 ```json ... ``` 區塊提取
    match = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if match:
        text = match.group(1).strip()

    # 嘗試直接從文字中找到 JSON 物件
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        text = json_match.group(0)

    try:
        data = json.loads(text)
        is_match = bool(data.get("is_match", True))
        reason = str(data.get("reason", "Match"))
        return is_match, reason
    except (json.JSONDecodeError, KeyError, TypeError):
        log.warning(
            f"[Node 5] Critic 回應解析失敗，預設為通過 (fail-open): "
            f"{raw_text[:200]}..."
        )
        return True, "JSON 解析失敗 — 預設通過"


# ===========================================================================
# Node 主函數
# ===========================================================================

def semantic_critic(state: AgentState) -> dict:
    """語意決審：比對 SQL 語意與使用者問題是否一致。"""
    champion_sql = state.get("champion_sql", "")
    user_query = state.get("user_query", "")

    log.info("[Node 5] Semantic Critic — 審查 SQL 語意")
    log.debug(f"  SQL: {champion_sql[:200]}...")

    if not champion_sql:
        return {
            "critic_passed": False,
            "critic_feedback": "沒有候選 SQL 可審查",
        }

    user_prompt = f"""【使用者原始需求】：{user_query}

【資料庫將執行的 SQL】：
{champion_sql}

請評估此 SQL 是否正確回答了使用者的需求。只輸出 JSON，不要有其他文字。"""

    try:
        response = llm_strong.invoke([
            SystemMessage(content=_build_critic_system_prompt()),
            HumanMessage(content=user_prompt),
        ])
        raw = response.content if hasattr(response, "content") else str(response)
        log.debug(f"[Node 5] Critic 原始回應: {raw[:300]}")
    except Exception as e:
        log.error(f"[Node 5] LLM 呼叫失敗: {e}")
        # LLM 失敗時預設通過（fail-open），避免系統卡死
        return {"critic_passed": True, "critic_feedback": ""}

    is_match, reason = _parse_critic_response(raw)

    if is_match:
        log.info("[Node 5] ✅ Critic 通過")
        return {"critic_passed": True, "critic_feedback": ""}
    else:
        retry = state.get("retry_count", 0) + 1
        log.warning(
            f"[Node 5] ❌ Critic 不通過 (retry_count→{retry}): {reason}"
        )
        return {
            "critic_passed": False,
            "critic_feedback": reason,
            "retry_count": retry,
        }
