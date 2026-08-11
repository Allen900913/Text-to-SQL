"""
Node 2: SQL Generator (精兵政策版)
====================================
使用 70B 模型一次生成 1 條最精準的 SQL。
放棄了原本的 batch(5) + Majority Voting 策略，
原因：NVIDIA NIM API 對大模型的並行請求有嚴格限制，會造成系統性超時卡死。
精兵政策的好處：
  1. 不再受 API 併發限制，速度大幅提升
  2. 70B 模型本身能力遠優於 8B，不需要投票來「糾正」低能力問題
  3. 配合升級後的 Semantic Critic，一條高品質 SQL 的準確率 > 5 條亂猜的 SQL
若有 critic_feedback（來自 Node 5 退回），會將反饋注入 Prompt 進行自我修正。
"""
import re
from loguru import logger as log
from langchain_core.messages import SystemMessage, HumanMessage

from langgraph_sql.state import AgentState
from langgraph_sql.config import llm_fast as llm_generator


# ===========================================================================
# Prompt 建構
# ===========================================================================

def _build_system_prompt(state: AgentState) -> str:
    """建構 System Prompt：包含 DDL、Enum、Business Rules。"""
    return f"""You are an expert MySQL SQL developer. Generate a valid MySQL SELECT query to answer the user's question.

Strictly follow these rules:
1. Output ONLY the SQL query. No explanation, no markdown code blocks, no comments.
2. Only use the tables and columns provided in the schema below.
3. Pay attention to Foreign Keys for JOINs.
4. Only generate SELECT or WITH (CTE) queries. Never generate INSERT, UPDATE, DELETE, DROP.
5. If the question asks for multiple pieces of information, combine them into a SINGLE SQL query using subqueries or JOINs. Do NOT split into multiple SQL statements.
6. Always respect the MySQL only_full_group_by mode: all non-aggregated columns in SELECT must appear in GROUP BY.

# Database Schema (DDL):
{state.get("schema_ddl", "")}

# Enum Fields (use exact English values):
{state.get("enum_text", "")}

# Business Rules (follow strictly):
{state.get("rules_text", "")}"""


def _build_user_prompt(state: AgentState) -> str:
    """建構 User Prompt：包含 Few-Shot 範例、使用者問題、Critic Feedback。"""
    parts: list[str] = []

    # Few-Shot 範例
    few_shot = state.get("few_shot_examples", "")
    if few_shot:
        parts.append("# Reference Examples (follow these SQL patterns):")
        parts.append(few_shot)

    # 使用者問題
    parts.append(f"# Target Question: {state.get('user_query', '')}")

    # Critic Feedback（若為重試）
    feedback = state.get("critic_feedback", "")
    if feedback:
        parts.append(
            f"\n# ⚠️ IMPORTANT — Previous attempt was REJECTED by the reviewer."
            f"\n# Rejection reason: {feedback}"
            f"\n# You MUST fix the SQL logic based on this feedback."
        )

    parts.append("\nSQL:")
    return "\n".join(parts)


# ===========================================================================
# SQL 解析：從 LLM 輸出中提取純 SQL
# ===========================================================================

def _parse_sql(raw_text: str) -> str | None:
    """
    從 LLM 回傳文字中提取純 SQL 語句。
    處理常見雜訊：markdown 包裹、前後說明文字等。
    """
    text = raw_text.strip()

    # 嘗試從 ```sql ... ``` 區塊提取
    match = re.search(r"```(?:sql)?\s*\n?(.*?)```", text, re.DOTALL | re.IGNORECASE)
    if match:
        text = match.group(1).strip()

    # 移除開頭的 "SQL:" 標記
    text = re.sub(r"^SQL:\s*", "", text, flags=re.IGNORECASE).strip()

    # 若有多行，只取到第一個空行或只含分號的行為止（避免取到說明文字）
    lines: list[str] = []
    for line in text.split("\n"):
        stripped = line.strip()
        # 遇到空行或純說明文字就停止
        if not stripped:
            if lines:  # 已有 SQL 內容，空行表示 SQL 結束
                break
            continue
        # 如果該行不像 SQL（純英文說明或中文），停止
        if lines and not stripped.startswith(("--", "/*")) and re.match(
            r"^[A-Z][a-z].*[.!]$", stripped
        ):
            break
        lines.append(line)

    text = "\n".join(lines).strip()

    # 移除尾部分號
    text = text.rstrip(";").strip()

    # 基本驗證：必須以 SELECT 或 WITH 開頭
    if text and text.upper().lstrip().startswith(("SELECT", "WITH")):
        return text

    return None


# ===========================================================================
# Node 主函數
# ===========================================================================

def sql_generator(state: AgentState) -> dict:
    """使用 70B 模型精準生成 1 條 SQL（精兵政策，取代舊的 batch(5) + 投票機制）。"""
    retry = state.get("retry_count", 0)
    log.info(f"[Node 2] SQL Generator (精兵政策) — 生成 1 條精準 SQL (retry={retry})")

    system_prompt = _build_system_prompt(state)
    user_prompt = _build_user_prompt(state)

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_prompt),
    ]

    # 單次精準呼叫，告別 batch 超時地獄
    try:
        response = llm_generator.invoke(messages)
        raw = response.content if hasattr(response, "content") else str(response)
    except Exception as e:
        log.error(f"[Node 2] LLM 呼叫失敗: {e}")
        return {
            "candidate_sqls": [],
            "error_message": f"SQL 生成失敗: {e}",
        }

    # 解析 SQL
    sql = _parse_sql(raw)
    if sql:
        log.info(f"[Node 2] ✅ 成功生成 SQL: {sql[:100]}...")
        return {"candidate_sqls": [sql]}
    else:
        log.warning(f"[Node 2] ❌ SQL 解析失敗 (raw={raw[:150]}...)")
        return {
            "candidate_sqls": [],
            "error_message": "SQL 解析失敗：LLM 輸出不含有效的 SELECT 語句",
        }
