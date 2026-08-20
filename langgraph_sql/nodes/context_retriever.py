"""
Node 1: Context Retriever
==========================
從 semantic_layer.yaml 載入 Schema DDL、Enum 欄位、商業規則、Few-Shot 範例，
並用語意檢索 + KMB 把 DDL 剪到「這一題真正需要的表」。不呼叫生成模型。

為什麼要剪：21 張表的完整 DDL 是 9,278 字元，佔整份 Prompt 的一半，但實測
145 題平均每題只需要 1.8 張表。模型每次都在讀十倍於它需要的 schema。

三段漏斗（各段的實測數字寫在對應的模組開頭）：
  收斂 —— 語意相似度把候選表收斂到 CANDIDATE_N 張。       table_retriever
  選表 —— LLM 從候選裡選出邏輯上必要的表。這一段是關鍵：   table_filter
          相似度知道「這題關於客戶」，但不知道「算總消費
          必須 JOIN orders」，那是結構必要性不是文字相似。
  補橋 —— KMB 補上橋接表。問「陳先生買了什麼手機」不會      schema_graph
          提到 order_items，只能靠外鍵結構找。

139 題實測：召回 99.3%，平均帶進 2.1 / 21 張表（10%）。
對照純相似度 K=4 的 91.4% / 4.9 張 —— 召回更高而且表更少。

最後 0.7% 的保險：把「其他表的名字」也附上（只有名字，不含欄位）。DDL 只是
Prompt，資料庫裡 21 張表全都在，EXPLAIN 驗的是真實 DB —— 所以模型只要知道
那張表存在就能用它。真正的風險是它不知道，然後拿看得到的表硬湊一個答案。
實測 #24 就是檢索漏了 orders/order_items，而模型靠這份表名清單自己正確補上。
"""
from loguru import logger as log

from langgraph_sql.state import AgentState
from langgraph_sql.utils.schema_graph import format_join_hints
from langgraph_sql.utils.schema_parser import get_schema_parser
from langgraph_sql.utils.schema_registry import get_table_columns
from langgraph_sql.utils.table_retriever import select_tables

# 只在 LLM 選表那一層失效時才會用到的退路。純相似度在 139 題上的表現：
#   K=3  召回 85.6% / 3.8 張表      K=4  召回 91.4% / 4.9 張表
#   K=6  召回 94.2% / 7.1 張表
# 取 K=4 是這條曲線上的折衷點。它不再是主要路徑 —— 為什麼固定 K 本身就是
# 死路（同一個 K 對半數題目慷慨五倍、對難題又不夠），寫在 table_filter 開頭。
RETRIEVAL_TOP_K = 4


def _other_tables_line(selected: set[str]) -> str:
    """列出沒被選中的表名。只有名字，不含欄位 —— 21 個名字約 250 字元。"""
    others = sorted(set(get_table_columns()) - {t.lower() for t in selected})
    if not others:
        return ""
    return ("\n-- 資料庫裡還有這些表（此處未列出欄位，需要時可直接使用）:\n"
            f"--   {', '.join(others)}")


def context_retriever(state: AgentState) -> dict:
    """載入 semantic_layer 內容，並把 DDL 剪成這一題需要的表。"""
    query = state.get("user_query", "")
    log.info("[Node 1] Context Retriever — 載入 Schema 資訊")
    log.info(f"[Node 1] 使用者問題: {query}")

    parser = get_schema_parser()
    all_tables = set(get_table_columns())

    tables, anchors = select_tables(query, top_k=RETRIEVAL_TOP_K)
    # 檢索失效時 select_tables 會回傳全部的表，此時不做任何剪裁
    scoped = None if tables >= all_tables else tables

    ddl = parser.get_ddl_for(scoped)
    if scoped:
        ddl += _other_tables_line(scoped)
        hints = format_join_hints(scoped)
        if hints:
            ddl += f"\n-- 這些表之間的外鍵關聯:\n{hints}"

    enum_text = parser.get_enum_text(scoped)
    rules_text = parser.get_rules_text()
    few_shot = parser.get_few_shot_text()

    if scoped:
        log.info(f"[Node 1] 檢索: 錨點={anchors} → {len(scoped)}/{len(all_tables)} 張表, "
                 f"DDL {len(parser.get_ddl())} → {len(ddl)} 字元")
    else:
        log.warning("[Node 1] 未剪裁，使用完整 DDL")

    log.debug(f"[Node 1] DDL 長度={len(ddl)}, Enum 長度={len(enum_text)}, "
              f"Rules 長度={len(rules_text)}, FewShot 長度={len(few_shot)}")

    return {
        "schema_ddl": ddl,
        "enum_text": enum_text,
        "rules_text": rules_text,
        "few_shot_examples": few_shot,
        "retrieved_tables": sorted(scoped) if scoped else sorted(all_tables),
        "retrieval_anchors": anchors,
    }
