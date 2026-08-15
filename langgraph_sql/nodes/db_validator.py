"""
Node 4: DB Validator
====================
以 MySQL EXPLAIN 驗證已通過 AST 安全快篩的 SQL。

MySQL 是 SQL 是否可執行的唯一裁判：EXPLAIN 成功才進入真正查詢；
失敗時保留資料庫原生錯誤，交由 SQL Generator 修復。
"""
import re

import sqlglot
from sqlglot import exp
from loguru import logger as log

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.state import AgentState
from langgraph_sql.utils.db_manager import get_db_manager
from langgraph_sql.utils.schema_registry import format_whitelist


# MySQL 的 schema 類錯誤只說「這個東西不存在」，不說「那有什麼」：
#   1054 Unknown column 'age' in 'field list'
#   1146 Table 'ecommerce_demo.shipments' doesn't exist
# 這是走 EXPLAIN 這條路徑的盲點 —— AST 那條路徑攔到幻覺欄位時會附上合法欄位清單，
# 但沒有表限定詞的欄位（SELECT age）在 AST 是 fail-open 放行的，只會死在這裡。
# 模型收到沒有清單的錯誤就只能再猜一次，等於白燒一輪重試預算。
_SCHEMA_ERROR_PATTERN = re.compile(
    r"Unknown column|doesn't exist|Unknown table", re.IGNORECASE
)


def _referenced_tables(sql: str) -> set[str] | None:
    """
    取出 SQL 實際引用的實體表名（排除 CTE 名稱）。

    只展開這幾張表的欄位，而不是整份 schema —— 錯誤訊息裡的白名單會隨表數
    線性膨脹，而模型要修的只是它自己寫的那幾張表。解析失敗時回 None，
    讓上層退回「只列表名」的保守輸出。
    """
    try:
        ast = sqlglot.parse_one(sql, read="mysql")
    except Exception:
        return None

    cte_names = {c.alias.lower() for c in ast.find_all(exp.CTE) if c.alias}
    return {
        t.name.lower()
        for t in ast.find_all(exp.Table)
        if t.name and t.name.lower() not in cte_names
    }


def _augment_schema_error(error: str, sql: str) -> str:
    """若錯誤屬於「找不到欄位/表」，補上相關表的白名單讓模型有依據可改。"""
    if not _SCHEMA_ERROR_PATTERN.search(error):
        return error
    return (
        f"{error}\n"
        f"資料庫實際存在的表與欄位如下，請只使用這些欄位：\n"
        f"{format_whitelist(_referenced_tables(sql))}\n"
        f"若使用者要的概念在上面找不到對應欄位，不要拼湊替代公式，"
        f"請改輸出：SELECT 'SCHEMA_UNSUPPORTED' AS system_error_flag;"
    )


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
            error = _augment_schema_error(
                f"MySQL EXPLAIN failed for SQL #{index}: {exc}", sql
            )
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
