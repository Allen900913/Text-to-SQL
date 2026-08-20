"""
從 INFORMATION_SCHEMA 產生 semantic_layer.yaml 的 ddl 區塊
============================================================
DDL 有 144 個欄位之後，手工維護 YAML 就是一個穩定的漂移來源 —— 而且
漂移的後果是靜默的：模型看到不存在的欄位會去用它，或看不到存在的欄位
而漏掉條件。schema_registry 已經把「白名單」與「外鍵」都改成向 MySQL 取，
DDL 是最後一塊還在手寫的。

這支程式把 DDL 也改成生成的。它讀取真實 schema 的欄位型別與 COMMENT，
產出與原本手寫版本相同排版的文字，覆寫進 utils/semantic_layer.yaml 的
ddl 欄位。COMMENT 本來就寫在 CREATE TABLE 裡（見 init_db.py 與
init_db_ext.py），所以生成版不會比手寫版少任何資訊。

執行：python tools/gen_ddl.py          # 預覽
      python tools/gen_ddl.py --write  # 寫回 semantic_layer.yaml
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "utils", "semantic_layer.yaml",
)

# 排序：核心交易表在前，週邊與雜項在後。模型從上往下讀，重要的先出現。
TABLE_ORDER = [
    "customers", "products", "orders", "order_items",
    "categories", "product_categories", "suppliers", "product_suppliers",
    "product_specs", "payment_methods", "payments", "refunds",
    "warehouses", "shipments", "addresses",
    "promotions", "order_promotions",
    "carts", "cart_items", "reviews", "browse_logs",
]

_COLUMNS_SQL = """
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_KEY, COLUMN_COMMENT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""

_TABLE_SQL = """
SELECT TABLE_NAME, TABLE_COMMENT
FROM INFORMATION_SCHEMA.TABLES
WHERE TABLE_SCHEMA = DATABASE()
"""

_FK_SQL = """
SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""


def build_ddl() -> str:
    db = get_db_manager(MYSQL_URI)
    with db.engine.connect() as conn:
        cols = conn.execute(text(_COLUMNS_SQL)).fetchall()
        fks = conn.execute(text(_FK_SQL)).fetchall()
        tbl_comments = {t: (c or "") for t, c in
                        conn.execute(text(_TABLE_SQL)).fetchall()}

    by_table: dict[str, list] = {}
    for t, c, typ, key, comment in cols:
        by_table.setdefault(t, []).append((c, typ.upper(), key, comment))

    fk_by_table: dict[str, list] = {}
    for t, c, rt, rc in fks:
        fk_by_table.setdefault(t, []).append((c, rt, rc))

    ordered = [t for t in TABLE_ORDER if t in by_table]
    ordered += [t for t in sorted(by_table) if t not in TABLE_ORDER]

    blocks = []
    for table in ordered:
        rows = by_table[table]
        name_w = max(len(c) for c, *_ in rows)
        type_w = max(len(t) for _, t, *_ in rows)

        lines = [f"CREATE TABLE {table} ("]
        body = []
        for col, typ, key, comment in rows:
            pk = " PRIMARY KEY" if key == "PRI" else ""
            note = f" COMMENT '{comment}'" if comment else ""
            body.append(f"  {col:<{name_w}} {typ:<{type_w}}{pk}{note}")
        for col, rt, rc in fk_by_table.get(table, []):
            body.append(f"  FOREIGN KEY ({col}) REFERENCES {rt}({rc})")
        lines.append(",\n".join(body))
        # 表級 COMMENT —— 2026-08-19 補上（ARCHITECTURE.md §10.7）。
        # 這些註解寫得很有防禦性（「即時庫存在 products.stock」、
        # 「想看訂單『現在』的狀態請用 orders.status」），但原本**只出現在
        # 檢索目錄**裡，也就是只有選表 LLM 看得到，生成 LLM 從來沒看過。
        # 干擾表補進 DDL 之後生成端第一次看得到 stock_qty 這種欄位，
        # 誘惑到位而警告沒到 —— 那會變成只有一道防線。
        comment = tbl_comments.get(table, "").replace("'", "''")
        lines.append(f") COMMENT '{comment}';" if comment else ");")
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) + "\n"


def main() -> int:
    ddl = build_ddl()
    n_tables = ddl.count("CREATE TABLE")
    print(f"產生 {n_tables} 張表的 DDL，共 {len(ddl):,} 字元\n")

    if "--write" not in sys.argv:
        print(ddl[:1500])
        print(f"\n...（預覽前 1500 字元）\n加 --write 才會寫回 {YAML_PATH}")
        return 0

    import io

    import yaml

    raw = io.open(YAML_PATH, encoding="utf-8").read()
    data = yaml.safe_load(raw)
    old = data["ddl"]

    # 只換掉 ddl 這一段，其餘（rules / few-shot / 檔頭註解）逐字保留 ——
    # 整份用 yaml.dump 重寫會把註解與排版全部弄丟。
    marker = "\nddl: |\n"
    start = raw.index(marker) + len(marker)
    indented_old = "\n".join(f"  {ln}" if ln else "" for ln in old.rstrip("\n").split("\n"))
    end = start + len(indented_old)
    if raw[start:end] != indented_old:
        print("找不到原本的 ddl 區塊（排版可能已改變），請手動處理")
        return 1

    indented_new = "\n".join(f"  {ln}" if ln else "" for ln in ddl.rstrip("\n").split("\n"))
    io.open(YAML_PATH, "w", encoding="utf-8").write(raw[:start] + indented_new + raw[end:])
    print(f"已寫回 {YAML_PATH}：ddl {len(old):,} → {len(ddl):,} 字元")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
