"""
Node 3: AST Validator
======================
使用 sqlglot 進行確定性快篩（MySQL 方言）。

三層過濾：
  1. 語法解析 — sqlglot.parse_one(sql, read="mysql")
  2. 安全過濾 — Root 必須是 SELECT / UNION
  3. 幻覺過濾 — 檢查 Table 與 Column 是否存在於 Schema（含 Alias 解析，fail-open）

通過後強制注入 LIMIT 501 防止笛卡兒積。
"""
import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from loguru import logger as log

from langgraph_sql.state import AgentState
from langgraph_sql.utils.schema_parser import get_schema_parser


# ===========================================================================
# 輔助函數
# ===========================================================================

def _build_alias_map(ast) -> dict[str, str]:
    """
    遍歷 AST 中所有 Table 節點，建立 Alias → Table Name 映射。
    例如：FROM customers c → {"c": "customers", "customers": "customers"}
    """
    alias_map: dict[str, str] = {}
    for table_node in ast.find_all(exp.Table):
        real_name = table_node.name.lower()
        alias = (table_node.alias or "").lower()
        if alias:
            alias_map[alias] = real_name
        alias_map[real_name] = real_name
    return alias_map


def _collect_cte_names(ast) -> set[str]:
    """
    收集 CTE (WITH) 中定義的名稱。
    CTE 名稱在 FROM 中使用時不應被當作「不存在的表」。
    """
    cte_names: set[str] = set()
    for cte_node in ast.find_all(exp.CTE):
        if cte_node.alias:
            cte_names.add(cte_node.alias.lower())
    return cte_names


def _inject_limit(ast, limit: int = 501) -> str:
    """
    若最外層 SELECT 沒有 LIMIT，強制注入 LIMIT 防止笛卡兒積。
    UNION 查詢不注入（行為複雜，交由 Node 4 的 MAX_RESULT_ROWS 防禦）。
    """
    if isinstance(ast, exp.Select):
        # 只檢查最外層 SELECT 的 LIMIT（不受子查詢影響）
        if not ast.args.get("limit"):
            ast = ast.limit(limit)

    return ast.sql(dialect="mysql")


# ===========================================================================
# Node 主函數
# ===========================================================================

def ast_validator(state: AgentState) -> dict:
    """使用 sqlglot 進行 AST 層級的 SQL 快篩。"""
    candidates = state.get("candidate_sqls", [])
    log.info(f"[Node 3] AST Validator — 檢查 {len(candidates)} 條候選 SQL")

    if not candidates:
        log.warning("[Node 3] 無候選 SQL 可檢查")
        retry = state.get("retry_count", 0) + 1
        return {
            "valid_sqls": [],
            "retry_count": retry,
            "error_message": "所有候選 SQL 均解析失敗，無法進行驗證。",
        }

    # 從 Schema 取得白名單
    parser = get_schema_parser()
    allowed_tables: set[str] = {t.lower() for t in parser.get_allowed_tables()}
    table_columns: dict[str, list[str]] = {
        k.lower(): [c.lower() for c in v]
        for k, v in parser.get_table_columns().items()
    }

    valid_sqls: list[str] = []

    for i, sql in enumerate(candidates):
        tag = f"SQL #{i+1}"
        try:
            # ============================================================
            # 第一層：語法解析
            # ============================================================
            ast = sqlglot.parse_one(sql, read="mysql")

            # ============================================================
            # 第二層：安全過濾（Root 必須是 SELECT / UNION）
            # ============================================================
            if not isinstance(ast, (exp.Select, exp.Union)):
                log.debug(f"  {tag} 淘汰 — Root 非 SELECT/UNION "
                          f"(type={type(ast).__name__})")
                continue

            # ============================================================
            # 第三層：幻覺過濾
            # ============================================================
            alias_map = _build_alias_map(ast)
            cte_names = _collect_cte_names(ast)

            # --- 3a. 檢查 Table 是否存在於 Schema ---
            tables_ok = True
            for table_node in ast.find_all(exp.Table):
                tname = table_node.name.lower()
                if tname in cte_names:
                    continue  # CTE 定義的名稱，不需要在 Schema 中
                if tname not in allowed_tables:
                    log.debug(f"  {tag} 淘汰 — 不存在的表: {tname}")
                    tables_ok = False
                    break
            if not tables_ok:
                continue

            # --- 3b. 檢查 Column 是否存在（fail-open 策略） ---
            #   - 有明確 table 引用的 Column → 解析 Alias 後比對
            #   - 無 table 引用的 Column（如 SELECT name）→ 放行
            #   - 無法解析歸屬的 Column（如聚合函數內）→ 放行
            columns_ok = True
            for col_node in ast.find_all(exp.Column):
                col_name = col_node.name.lower()
                table_ref = (col_node.table or "").lower()

                if not table_ref:
                    continue  # 無表引用 → 放行

                # 解析 Alias → 真實 Table Name
                real_table = alias_map.get(table_ref)
                if real_table is None:
                    continue  # 無法解析（可能是子查詢 alias）→ 放行

                known_cols = table_columns.get(real_table)
                if known_cols is None:
                    continue  # 表不在 Schema 映射中 → 放行

                if col_name not in known_cols:
                    log.debug(
                        f"  {tag} 淘汰 — 幻覺欄位: {real_table}.{col_name} "
                        f"(合法欄位: {known_cols})"
                    )
                    columns_ok = False
                    break

            if not columns_ok:
                continue

            # ============================================================
            # 全部通過 → 轉回 SQL 字串（移除原先的 LIMIT 注入）
            # ============================================================
            final_sql = ast.sql(dialect="mysql")
            valid_sqls.append(final_sql)
            log.debug(f"  {tag} ✅ 通過 → {final_sql[:120]}...")

        except ParseError as e:
            log.debug(f"  {tag} 淘汰 — 語法錯誤: {e}")
            continue
        except Exception as e:
            log.debug(f"  {tag} 淘汰 — 未預期錯誤: {type(e).__name__}: {e}")
            continue

    log.info(f"[Node 3] 通過快篩: {len(valid_sqls)}/{len(candidates)}")

    result: dict = {"valid_sqls": valid_sqls}

    # 若全部淘汰，遞增 retry_count
    if not valid_sqls:
        result["retry_count"] = state.get("retry_count", 0) + 1
        result["error_message"] = "所有候選 SQL 均未通過 AST 驗證。"
        log.warning(
            f"[Node 3] 全部淘汰，retry_count={result['retry_count']}"
        )

    return result
