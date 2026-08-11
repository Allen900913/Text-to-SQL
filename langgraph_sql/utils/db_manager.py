"""
資料庫管理器
============
基於原有 db_utils.py 重構，加入：
- MySQL max_execution_time 超時控制
- pandas DataFrame 回傳功能
- 安全的唯讀查詢防護
"""
import json
import threading
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from loguru import logger as log


FORBIDDEN_KEYWORDS = [
    "insert", "update", "delete", "drop", "alter",
    "truncate", "create", "grant", "revoke",
]


class DatabaseManager:
    """MySQL 資料庫管理器，提供安全的唯讀查詢功能。"""

    def __init__(self, connection_string: str):
        self.engine = create_engine(
            connection_string,
            pool_size=5,
            pool_recycle=3600,
        )

    # ------------------------------------------------------------------
    # 安全檢查
    # ------------------------------------------------------------------
    def _check_readonly(self, sql: str) -> None:
        """確認 SQL 為唯讀操作。"""
        sql_lower = sql.lower().strip()
        if not sql_lower.startswith(("select", "with", "explain")):
            for kw in FORBIDDEN_KEYWORDS:
                if kw in sql_lower:
                    raise ValueError(f"安全限制：禁止執行包含 '{kw}' 的操作")

    # ------------------------------------------------------------------
    # 核心方法：回傳 DataFrame（供 Node 4 使用）
    # ------------------------------------------------------------------
    def execute_to_dataframe(
        self, sql: str, timeout_ms: int = 5000
    ) -> pd.DataFrame:
        """
        執行 SQL 並回傳 pandas DataFrame。
        使用 MySQL SET SESSION max_execution_time 進行超時控制。

        Args:
            sql: 要執行的 SQL 語句
            timeout_ms: 超時時間（毫秒），預設 5000ms

        Returns:
            pd.DataFrame: 查詢結果
        """
        self._check_readonly(sql)

        with self.engine.connect() as conn:
            # 設定查詢超時（MySQL 標準，單位毫秒）
            conn.execute(text(f"SET SESSION max_execution_time = {timeout_ms}"))
            try:
                df = pd.read_sql_query(text(sql), conn)
                return df
            except Exception:
                raise
            finally:
                # 重置超時，避免影響連線池中的其他操作
                try:
                    conn.execute(text("SET SESSION max_execution_time = 0"))
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # JSON 格式回傳（供 Node 6 的結果展示使用）
    # ------------------------------------------------------------------
    def execute_query_json(self, sql: str, limit: int = 100) -> str:
        """執行 SQL 並回傳 JSON 字串。"""
        self._check_readonly(sql)

        with self.engine.connect() as conn:
            result = conn.execute(text(sql))
            columns = result.keys()
            rows = result.fetchmany(limit)

            if not rows:
                return "[]"

            result_data = []
            for row in rows:
                row_dict = {}
                for i, col in enumerate(columns):
                    try:
                        val = row[i]
                        if val is not None:
                            json.dumps(val)
                        row_dict[col] = val
                    except (TypeError, ValueError):
                        row_dict[col] = str(row[i])
                result_data.append(row_dict)

            return json.dumps(result_data, ensure_ascii=False, indent=2)

    def close(self):
        """關閉資料庫連線。"""
        self.engine.dispose()


# ===========================================================================
# 全域單例（惰性初始化，Double-checked Locking）
# ===========================================================================
_db: DatabaseManager | None = None
_db_lock = threading.Lock()


def get_db_manager(connection_string: str) -> DatabaseManager:
    """取得（或建立）全域 DatabaseManager 單例。"""
    global _db
    if _db is None:
        with _db_lock:
            if _db is None:
                _db = DatabaseManager(connection_string)
                log.info("資料庫連線初始化完成")
    return _db
