"""
Schema Registry — 欄位白名單的權威來源
=======================================
白名單直接向 MySQL 的 INFORMATION_SCHEMA 取得，而不是用 regex 去剖析
semantic_layer.yaml 裡的 DDL 文字。理由有二：

  1. 不會漂移：YAML 是手寫的，DB 改了而 YAML 沒改，過濾器就會擋掉合法欄位、
     或放行已經不存在的欄位。INFORMATION_SCHEMA 永遠等於真實 schema。
  2. 不會靜默失效：regex 遇到反引號、IF NOT EXISTS、缺分號的 DDL 會回空字典，
     而 ast_validator 的欄位檢查是 fail-open 的 —— 空字典代表整層幻覺過濾
     悄悄停止運作，log 上一片正常。改讀結構化的 metadata 就沒有剖析這一步。

YAML 的 DDL 文字仍然保留：它有中文 COMMENT，是餵給 LLM 的 schema 說明。
這裡改的只是「驗證器拿什麼當白名單」。啟動時會對帳兩者，不一致就 log 警告，
等於免費得到一個 schema 漂移偵測。
"""
import threading
from collections.abc import Iterable

from loguru import logger as log
from sqlalchemy import text

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager
from langgraph_sql.utils.schema_parser import get_schema_parser


_INFORMATION_SCHEMA_SQL = """
SELECT TABLE_NAME, COLUMN_NAME
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

_table_columns: dict[str, list[str]] | None = None
_lock = threading.Lock()


def _load_from_information_schema() -> dict[str, list[str]]:
    """向 MySQL 查詢真實 schema，組成 {table: [columns]}。"""
    db = get_db_manager(MYSQL_URI)
    with db.engine.connect() as conn:
        rows = conn.execute(text(_INFORMATION_SCHEMA_SQL)).fetchall()

    columns: dict[str, list[str]] = {}
    for table_name, column_name in rows:
        columns.setdefault(table_name.lower(), []).append(column_name.lower())
    return columns


def _report_drift(live: dict[str, list[str]]) -> None:
    """對帳 YAML DDL 與真實 schema，只警告不阻斷。"""
    yaml_cols = {
        k.lower(): {c.lower() for c in v}
        for k, v in get_schema_parser().get_table_columns().items()
    }
    live_cols = {k: set(v) for k, v in live.items()}

    only_db = sorted(set(live_cols) - set(yaml_cols))
    only_yaml = sorted(set(yaml_cols) - set(live_cols))
    if only_db:
        log.warning(f"[Schema] DB 有但 YAML DDL 沒有的表: {', '.join(only_db)}")
    if only_yaml:
        log.warning(f"[Schema] YAML DDL 有但 DB 沒有的表: {', '.join(only_yaml)}")

    for table in sorted(set(live_cols) & set(yaml_cols)):
        missing = sorted(live_cols[table] - yaml_cols[table])
        extra = sorted(yaml_cols[table] - live_cols[table])
        if missing:
            log.warning(f"[Schema] {table}: DB 有但 YAML DDL 未描述的欄位 {missing}"
                        "（LLM 看不到這些欄位，考慮補進 semantic_layer.yaml）")
        if extra:
            log.warning(f"[Schema] {table}: YAML DDL 描述了但 DB 不存在的欄位 {extra}"
                        "（LLM 會被誤導去用這些欄位）")


def get_table_columns() -> dict[str, list[str]]:
    """
    取得 {table: [columns]} 白名單，全部小寫。首次呼叫時查 DB 並快取。

    若 DB 不可用則退回 YAML regex 剖析 —— 這是降級而非等價替代，
    所以會明確 log 出來，不讓它悄悄發生。
    """
    global _table_columns
    if _table_columns is not None:
        return _table_columns

    with _lock:
        if _table_columns is not None:
            return _table_columns
        try:
            live = _load_from_information_schema()
            if not live:
                raise RuntimeError("INFORMATION_SCHEMA 回傳 0 列，目標 database 可能是空的")
            _report_drift(live)
            log.info(f"[Schema] 白名單來源 = INFORMATION_SCHEMA，"
                     f"{len(live)} 張表 / {sum(len(v) for v in live.values())} 個欄位")
            _table_columns = live
        except Exception as e:
            log.error(f"[Schema] 無法從 INFORMATION_SCHEMA 取得白名單（{type(e).__name__}: {e}），"
                      "降級改用 YAML DDL 的 regex 剖析結果")
            _table_columns = {
                k.lower(): [c.lower() for c in v]
                for k, v in get_schema_parser().get_table_columns().items()
            }
        return _table_columns


def get_allowed_tables() -> set[str]:
    """取得合法表名集合，全部小寫。"""
    return set(get_table_columns().keys())


def format_whitelist(tables: Iterable[str] | None = None) -> str:
    """
    把白名單格式化成一行一表的文字，供錯誤訊息附帶給 LLM 參考。

    tables 指定時只展開這幾張表的欄位，其餘表僅列表名。這個表限定是必要的
    而非優化：每個欄位在這裡約佔 12 字元，4 張表 / 22 欄時整份約 200 字元無妨，
    但 20 張表 / 200 欄時會膨脹到約 2,500 字元，塞進錯誤訊息會把後面真正要
    模型做的修正指示稀釋掉。模型要修的是某張表的某個欄位，不需要看完整個資料庫。

    tables 為 None（或指定的表全都不存在）時退回只列出所有表名 —— 那代表
    連該看哪張表都不確定，給表名讓模型自己指認，比灌一份全表欄位有效。
    """
    all_cols = get_table_columns()
    focus = set(all_cols) if tables is None else {t.lower() for t in tables} & set(all_cols)

    lines = [f"  {table}: {', '.join(all_cols[table])}" for table in sorted(focus)]

    others = sorted(set(all_cols) - focus)
    if others:
        label = "資料庫還有這些表" if focus else "資料庫裡實際存在的表"
        lines.append(f"  （{label}，需要時再引用其欄位）: {', '.join(others)}")

    return "\n".join(lines)
