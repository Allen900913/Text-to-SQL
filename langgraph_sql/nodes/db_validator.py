"""
Node 4: DB Validator
====================
以 MySQL EXPLAIN 驗證已通過 AST 安全快篩的 SQL。

MySQL 是 SQL 是否可執行的唯一裁判：EXPLAIN 成功才進入真正查詢；
失敗時保留資料庫原生錯誤，交由 SQL Generator 修復。
"""
from loguru import logger as log

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.state import AgentState
from langgraph_sql.utils.db_manager import get_db_manager


def db_validator(state: AgentState) -> dict:
    """使用 MySQL EXPLAIN 驗證 SQL 是否可被資料庫接受。"""
    valid_sqls = state.get("valid_sqls", [])
    log.info(f"[Node 4] DB Validator — EXPLAIN 驗證 {len(valid_sqls)} 條 SQL")

    if not valid_sqls:
        return {
            "sql_validated": False,
            "db_error": "沒有通過 AST 驗證的 SQL 可以交給 MySQL EXPLAIN。",
        }

    # 現行 Generator 一次只生成一條 SQL；保留迴圈以兼容未來多候選策略。
    db = get_db_manager(MYSQL_URI)
    errors: list[str] = []
    explain_passed: list[str] = []

    for index, sql in enumerate(valid_sqls, start=1):
        try:
            db.explain_query(sql)
            explain_passed.append(sql)
            log.debug(f"  SQL #{index} ✅ EXPLAIN 通過")
        except Exception as exc:
            # str(exc) 含 MySQL error code/message，可直接供 Generator 修復。
            error = f"MySQL EXPLAIN failed for SQL #{index}: {exc}"
            errors.append(error)
            log.debug(f"  SQL #{index} ❌ {error[:300]}")

    if explain_passed:
        return {
            "valid_sqls": explain_passed,
            "sql_validated": True,
            "db_error": "",
            "error_message": "",
        }

    db_error = "\n".join(errors)
    retry = state.get("retry_count", 0) + 1
    log.warning(f"[Node 4] EXPLAIN 全部失敗，retry_count={retry}")
    return {
        "valid_sqls": [],
        "sql_validated": False,
        "db_error": db_error,
        "retry_count": retry,
        "error_message": "MySQL EXPLAIN 無法驗證生成的 SQL。",
    }
