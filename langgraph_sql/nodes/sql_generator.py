"""
Node 2: SQL Generator (精兵政策版)
====================================
使用 70B 模型一次生成 1 條最精準的 SQL。
放棄了原本的 batch(5) + Majority Voting 策略，
原因：NVIDIA NIM API 對大模型的並行請求有嚴格限制，會造成系統性超時卡死。
精兵政策的好處：
  1. 不再受 API 併發限制，速度大幅提升
  2. 70B 模型本身能力遠優於 8B，不需要投票來「糾正」低能力問題
  3. 透過 MySQL EXPLAIN 的原生錯誤進行 SQL 自我修復
若有 db_error（來自 AST、EXPLAIN 或實際執行），會將錯誤注入 Prompt 進行修正。
"""
import re
from loguru import logger as log
from langchain_core.messages import SystemMessage, HumanMessage

from langgraph_sql.state import AgentState
from langgraph_sql.config import llm_fast as llm_generator
from langgraph_sql.utils.llm_retry import invoke_with_retry


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

【嚴格業務邏輯邊界 (Strict Schema Constraints)】
1. 絕不可發明欄位：你只能使用 schema_ddl 中確實存在的欄位。
2. 絕不可發明公式：如果使用者的問題涉及未知的業務概念（如：運費、利潤率），且該概念沒有定義在 rules_text 中，你「絕對不可」自行推導或猜測計算公式（例如不可自行用 總金額-成本 當作運費）。
3. 觸發防禦機制：如果發現使用者的問題無法用現有 schema 與 rules_text 誠實回答，請不要試圖拼湊 SQL，請「強制」輸出以下這句標準 SQL 作為唯一回傳值：
   SELECT 'SCHEMA_UNSUPPORTED' AS system_error_flag;

# Database Schema (DDL):
{state.get("schema_ddl", "")}

# Enum Fields (use exact English values):
{state.get("enum_text", "")}

# Business Rules (follow strictly):
{state.get("rules_text", "")}"""


def _build_user_prompt(state: AgentState) -> str:
    """建構 User Prompt：包含 Few-Shot 範例、問題與資料庫驗證錯誤。"""
    parts: list[str] = []

    # Few-Shot 範例
    few_shot = state.get("few_shot_examples", "")
    if few_shot:
        parts.append("# Reference Examples (follow these SQL patterns):")
        parts.append(few_shot)

    # 使用者問題
    parts.append(f"# Target Question: {state.get('user_query', '')}")

    # DB/AST 驗證錯誤（若為重試）
    db_error = state.get("db_error", "")
    previous_sqls = state.get("candidate_sqls", [])
    previous_sql = previous_sqls[0] if previous_sqls else ""
    if db_error:
        parts.append(
            "\n# IMPORTANT — The previous SQL failed deterministic validation."
            f"\n# Previous SQL:\n{previous_sql}"
            f"\n# Database validation error:\n{db_error}"
            "\n# Rewrite the SQL to resolve this error. Preserve the user's request; "
            "output only one valid MySQL SELECT/WITH statement."
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

    # 單次精準呼叫，告別 batch 超時地獄。
    # API 逾時 / 503 由 invoke_with_retry 就地退避重試，不消耗 Pipeline 的 retry 預算。
    raw, llm_error = invoke_with_retry(llm_generator, messages, tag="[Node 2] Generator")
    if llm_error:
        return {
            "candidate_sqls": [],
            "llm_error": llm_error,
            "error_message": f"SQL 生成失敗（LLM API 無回應）: {llm_error}",
        }

    # 解析 SQL
    sql = _parse_sql(raw)
    if sql:
        log.info(f"[Node 2] ✅ 成功生成 SQL: {sql[:100]}...")
        return {"candidate_sqls": [sql], "llm_error": ""}
    else:
        log.warning(f"[Node 2] ❌ SQL 解析失敗 (raw={raw[:150]}...)")
        return {
            "candidate_sqls": [],
            # 這一輪 API 是正常的（有拿到內容，只是不是 SQL），要清掉前一輪殘留的
            # llm_error，否則評估會把模型的問題誤記成基礎設施故障。
            "llm_error": "",
            "error_message": "SQL 解析失敗：LLM 輸出不含有效的 SELECT 語句",
        }
