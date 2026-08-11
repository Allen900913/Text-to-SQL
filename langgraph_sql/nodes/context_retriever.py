"""
Node 1: Context Retriever
==========================
從 semantic_layer.yaml 載入 Schema DDL、Enum 欄位、商業規則、Few-Shot 範例。
純確定性操作，不呼叫 LLM。
"""
from loguru import logger as log

from langgraph_sql.state import AgentState
from langgraph_sql.utils.schema_parser import get_schema_parser


def context_retriever(state: AgentState) -> dict:
    """將 semantic_layer.yaml 的內容載入至 State。"""
    log.info(f"[Node 1] Context Retriever — 載入 Schema 資訊")
    log.info(f"[Node 1] 使用者問題: {state.get('user_query', '')}")

    parser = get_schema_parser()

    ddl = parser.get_ddl()
    enum_text = parser.get_enum_text()
    rules_text = parser.get_rules_text()
    few_shot = parser.get_few_shot_text()

    log.debug(f"[Node 1] DDL 長度={len(ddl)}, Enum 長度={len(enum_text)}, "
              f"Rules 長度={len(rules_text)}, FewShot 長度={len(few_shot)}")

    return {
        "schema_ddl": ddl,
        "enum_text": enum_text,
        "rules_text": rules_text,
        "few_shot_examples": few_shot,
    }
