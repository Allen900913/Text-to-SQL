"""
產生檢索用的表描述（階段一：LLM 合成增強）
====================================================================
為什麼需要這一步：

檢索層原本用「表名 + 欄位名 + 欄位註解」當作可被語意比對的文件。實測 145 題
K=3 召回率只有 85.6%，而失敗的 20 題裡有 19 題漏的是同一張表：order_items。

原因很具體：問題用的是**動詞**，文件寫的是**名詞**。
  問題：「哪一個商品類別的總營收最高？」「各商品類別各賣出多少件？」
  文件：「表 order_items：訂單明細　欄位：quantity（數量）、unit_price（單價）」
「營收」「賣出」「買過」在文件裡一個字都沒有，於是檢索永遠命中不了它，
反而把 browse_logs、product_specs、reviews 這些名詞相近的表抓成錨點。

這支程式讓 LLM 讀真實 DDL 與幾列樣本，替每張表寫出「它是拿來回答什麼問題的」，
包含典型問法。產物是純檢索用的 metadata，不會進生成模型的 Prompt ——
它不是商業規則，改它不會改變 SQL 怎麼寫，只改變哪些表被撈出來。

輸出：utils/table_docs.yaml
執行：python tools/gen_table_docs.py           # 全部重生
      python tools/gen_table_docs.py orders    # 只重生指定的表
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI, llm_fast  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "utils", "table_docs.yaml",
)

PROMPT = """你正在為一個繁體中文的 Text-to-SQL 系統建立「表檢索」用的描述。

這份描述的唯一用途，是讓使用者的自然語言問題能用語意相似度命中正確的表。
它不會被拿去生成 SQL，所以不要寫任何 SQL 語法或 JOIN 建議。

下面是一張表的結構與幾列真實資料：

{ddl}

樣本資料（最多 3 列）：
{sample}

請輸出兩段，不要有其他任何文字：

用途：一到兩句話說明這張表記錄什麼、在業務上代表什麼事件或實體。
問法：六個使用者可能會問、而且**必須用到這張表**才答得出來的問題，每個一行，
      用「- 」開頭。

規則（很重要，這些描述是拿去跟真實使用者的問題做語意比對的）：
  1. 絕對不要出現 ID、編號、欄位名。使用者不會問「商品 ID 為 7 的單價」，
     他們問的是「iPhone 賣了多少錢」。
  2. 多用商業語彙與動詞：營收、銷量、賣出、熱賣、暢銷、佔比、退款、
     出貨、送達、瀏覽、加入購物車、放棄。欄位名都是名詞，而使用者用動詞
     發問 —— 動詞沒寫進來，這張表就永遠檢索不到。
  3. 六個問法要涵蓋不同的說法，不要只是同一句話換字。
  4. 不要寫需要其他表才能回答的問題。
"""


def fetch(conn, sql: str, **kw):
    return conn.execute(text(sql), kw).fetchall()


def build_context(conn, table: str) -> tuple[str, str]:
    cols = fetch(conn, """
        SELECT COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t
        ORDER BY ORDINAL_POSITION
    """, t=table)
    comment = fetch(conn, """
        SELECT TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t
    """, t=table)[0][0]

    lines = [f"表名：{table}", f"表說明：{comment or '（無）'}", "欄位："]
    lines += [f"  {c} {t}{'  -- ' + (m or '') if m else ''}" for c, t, m in cols]
    ddl = "\n".join(lines)

    rows = fetch(conn, f"SELECT * FROM `{table}` LIMIT 3")
    names = [c for c, _, _ in cols]
    sample = "\n".join(
        "  " + ", ".join(f"{n}={v}" for n, v in zip(names, row)) for row in rows
    ) or "  （無資料）"
    return ddl, sample


def main() -> int:
    only = {t.lower() for t in sys.argv[1:]}
    db = get_db_manager(MYSQL_URI)

    with db.engine.connect() as conn:
        tables = [t for (t,) in fetch(conn, """
            SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME
        """)]
        targets = [t for t in tables if not only or t.lower() in only]

        existing: dict = {}
        if os.path.exists(OUT_PATH):
            with open(OUT_PATH, encoding="utf-8") as f:
                existing = yaml.safe_load(f) or {}

        for i, table in enumerate(targets, 1):
            ddl, sample = build_context(conn, table)
            print(f"[{i}/{len(targets)}] {table} ... ", end="", flush=True)
            reply = llm_fast.invoke(PROMPT.format(ddl=ddl, sample=sample)).content.strip()
            existing[table] = reply
            print(f"{len(reply)} 字元")

    # 依表名排序寫回，讓 diff 穩定
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write("# 檢索用的表描述 —— 由 tools/gen_table_docs.py 產生，不要手改。\n")
        f.write("# 這是給向量檢索比對用的 metadata，不會進生成模型的 Prompt。\n")
        f.write("# 重生：python tools/gen_table_docs.py [表名...]\n\n")
        yaml.safe_dump(dict(sorted(existing.items())), f,
                       allow_unicode=True, sort_keys=True, width=100,
                       default_style="|")

    print(f"\n已寫入 {OUT_PATH}（共 {len(existing)} 張表）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
