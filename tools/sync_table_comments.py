"""
把 init 腳本裡的**表註解與欄位註解**同步到現有資料庫（不動任何資料）
====================================================================
為什麼需要這支程式：

表註解是檢索層的主要訊號來源之一 —— table_retriever 拿
「表註解 + 欄位註解」組成可被語意比對的文件。實測發現最早那 4 張基礎表的
註解只是把表名複述一遍（order_items 的註解就是「訂單明細表」五個字），
而檢索唯一持續找不到的表正是 order_items。寫得好的表檢索找得到，
偷懶的表就找不到 —— 這是直接因果，不是巧合。

改註解本來只要重跑 init_db.py，但那會重新產生資料，而
eval_ground_truth.yaml 的預期答案全部綁在現有資料上。
所以這支程式改走 ALTER TABLE —— 純 metadata 操作，一個位元的資料都不會動。

唯一來源仍然是 CREATE TABLE 的宣告：這裡只負責把它們搬到已存在的資料庫，
不自己定義任何文字。

--------------------------------------------------------------------
2026-08-19 兩處擴充（ARCHITECTURE.md §10.6）：

  1. **加入 tools/add_distractor_tables.py 當來源。** 那 19 張表的註解原本
     沒有任何同步路徑 —— 與「DDL 從來沒有重新產生過」是同一類 bug：
     **新增了一個 schema 來源，卻沒有把它接到既有的同步鏈上。**

  2. **欄位註解也同步。** 原本只同步表註解，欄位註解改了不會生效。
     `#169` 就是這樣：`payment_attempts.result` 的註解只列出值域
     `(OK/DECLINED/TIMEOUT)` 卻沒說哪些算失敗，模型只算 DECLINED 得到 29，
     正解 37 要含 TIMEOUT。那不是模型錯，是定義沒有寫在定義該在的地方。

  欄位的 ALTER 用 `MODIFY COLUMN`，型別 / NULL / 預設值 / AUTO_INCREMENT
  **全部從 INFORMATION_SCHEMA 的現況重建**，不從 init 腳本解析 ——
  只有註解那一段來自宣告。這樣就不可能因為解析錯而改到欄位語意。

執行：python tools/sync_table_comments.py            # 只比對，不寫入
      python tools/sync_table_comments.py --apply    # 實際套用
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES = [os.path.join(ROOT, "db", "init_db.py"),
           os.path.join(ROOT, "db", "init_db_ext.py")]
# 這幾支用 Python tuple 宣告 CREATE TABLE，不是 SQL 文字，所以另外解析。
# **加了新的建表腳本一定要登記在這裡** —— 漏登記的症狀是「改了註解沒生效」，
# 而且不會報錯（§10.6 就是這樣被咬的）。tools/check_schema_pipeline.py
# 的第 3 項會抓到不一致，但抓到的是結果，登記在這裡才是原因。
TUPLE_SRCS = [
    os.path.join(ROOT, "tools", "add_distractor_tables.py"),
    os.path.join(ROOT, "tools", "add_domain_tables.py"),
    os.path.join(ROOT, "tools", "add_domain_tables_w2.py"),
]

# CREATE TABLE ... ) COMMENT '...';  —— 兩支 init 腳本都是這個排版
_PATTERN = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\(([^;]*?)\)\s*COMMENT\s*'([^']*)'\s*;",
    re.S,
)

# add_distractor_tables.py 的 TABLES 是 (表名, 表註解, \"\"\"欄位區塊\"\"\") 的 list，
# 不是 SQL 文字，所以要另外一條 pattern。刻意不 import 那支程式 ——
# 它 import 就會連資料庫。
_DISTRACTOR_PATTERN = re.compile(
    r'\(\s*"(\w+)"\s*,\s*"([^"]*)"\s*,\s*"""(.*?)"""\s*\)', re.S)

# 欄位行：開頭是欄位名，行內某處有 COMMENT '...'。
# FOREIGN KEY / PRIMARY KEY 這種約束行沒有 COMMENT，自然不會命中。
_COL_PATTERN = re.compile(r"^\s*(\w+)\s+[^,]*?COMMENT\s*'([^']*)'", re.M)


def _columns_of(body: str) -> dict[str, str]:
    return {c.lower(): cm for c, cm in _COL_PATTERN.findall(body)
            if c.lower() not in ("foreign", "primary", "unique", "key", "index")}


def declared() -> tuple[dict[str, str], dict[tuple[str, str], str]]:
    """回傳 ({表: 表註解}, {(表, 欄位): 欄位註解})。"""
    tables: dict[str, str] = {}
    columns: dict[tuple[str, str], str] = {}

    for path in SOURCES:
        with open(path, encoding="utf-8") as f:
            for table, body, comment in _PATTERN.findall(f.read()):
                t = table.lower()
                tables[t] = comment
                for col, cm in _columns_of(body).items():
                    columns[(t, col)] = cm

    for path in TUPLE_SRCS:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for table, comment, body in _DISTRACTOR_PATTERN.findall(f.read()):
                t = table.lower()
                tables[t] = comment
                for col, cm in _columns_of(body).items():
                    columns[(t, col)] = cm

    return tables, columns


def _column_def(row) -> str:
    """從 INFORMATION_SCHEMA 現況重建欄位定義（註解除外）。

    刻意不解析 init 腳本的型別文字：MODIFY COLUMN 會用這串話整個覆寫欄位，
    少寫一個 NOT NULL 就是一次靜默的 schema 變更。以現況為準，
    就只有註解會變。
    """
    parts = [row.COLUMN_TYPE]
    if row.IS_NULLABLE == "NO":
        parts.append("NOT NULL")
    if row.COLUMN_DEFAULT is not None:
        parts.append(f"DEFAULT {row.COLUMN_DEFAULT}")
    elif row.IS_NULLABLE == "YES":
        parts.append("DEFAULT NULL")
    extra = (row.EXTRA or "").strip()
    if extra:
        parts.append(extra.upper())
    return " ".join(parts)


def main() -> int:
    apply = "--apply" in sys.argv
    dec_tables, dec_cols = declared()
    db = get_db_manager(MYSQL_URI)

    with db.engine.connect() as conn:
        live_tables = {t.lower(): c for t, c in conn.execute(text(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"
        ))}
        live_cols = {}
        for row in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE, "
            "       COLUMN_DEFAULT, EXTRA, COLUMN_COMMENT "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"
        )):
            live_cols[(row.TABLE_NAME.lower(), row.COLUMN_NAME.lower())] = row

        missing = sorted(set(live_tables) - set(dec_tables))
        t_diff = {t: c for t, c in dec_tables.items()
                  if t in live_tables and live_tables[t] != c}
        c_diff = {k: c for k, c in dec_cols.items()
                  if k in live_cols and live_cols[k].COLUMN_COMMENT != c}

        print(f"宣告 {len(dec_tables)} 張表 / {len(dec_cols)} 個欄位註解；"
              f"資料庫有 {len(live_tables)} 張表 / {len(live_cols)} 個欄位")
        if missing:
            print(f"  ⚠️ 這些表在 init 腳本裡找不到註解宣告: {missing}")

        if not t_diff and not c_diff:
            print("\n表註解與欄位註解都已一致，無需變更。")
            return 0

        if t_diff:
            print(f"\n有 {len(t_diff)} 張表的註解與宣告不同:")
            for table, comment in sorted(t_diff.items()):
                print(f"  {table}\n    現在: {live_tables[table]}\n    應為: {comment}")
        if c_diff:
            print(f"\n有 {len(c_diff)} 個欄位的註解與宣告不同:")
            for (table, col), comment in sorted(c_diff.items()):
                print(f"  {table}.{col}\n"
                      f"    現在: {live_cols[(table, col)].COLUMN_COMMENT}\n"
                      f"    應為: {comment}")

        if not apply:
            print("\n這是預覽。加 --apply 才會實際執行 ALTER TABLE。")
            return 0

        for table, comment in sorted(t_diff.items()):
            # ALTER TABLE ... COMMENT 只改 metadata，不重建表、不動資料
            conn.execute(text(f"ALTER TABLE `{table}` COMMENT = :c"), {"c": comment})
        for (table, col), comment in sorted(c_diff.items()):
            definition = _column_def(live_cols[(table, col)])
            conn.execute(text(
                f"ALTER TABLE `{table}` MODIFY COLUMN `{col}` {definition} COMMENT :c"
            ), {"c": comment})
        conn.commit()
        print(f"\n已套用 {len(t_diff)} 張表註解、{len(c_diff)} 個欄位註解。")

    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
