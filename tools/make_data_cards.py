# -*- coding: utf-8 -*-
"""出題用的資料卡 —— 只有結構與真實資料，**沒有任何註解**。

為什麼要刻意拿掉註解
================================================================
新題庫的用途是量「這個架構在沒見過的題上有多準」。如果出題時看著
schema 註解寫問句，問句的用詞就會跟註解逐字對應，於是量到的是
「註解抄得像不像」而不是「架構好不好」——
現有 305 題的 `#282`／`#292` 就是這個形狀（問句與欄位註解幾乎逐字相同），
它們讓欄位提示那一層看起來比實際強（memory:
`comment-examples-from-data-not-questions` 的反向）。

隔離要靠結構不靠自律：出題的人（或模型）只拿得到這份卡，
拿不到 `TABLE_COMMENT`／`COLUMN_COMMENT`／`table_semantics.yaml`。

卡片裡有什麼
    · 表名、列數
    · 欄位名、型別、可空、相異值數
    · 低基數欄位的**真實值分布**（前 8 種，含筆數）
    · 高基數文字欄位的**真實樣本**（3 筆，讓出題能指名具體實體）
    · 外鍵：這張表連到誰
欄位名本身留著 —— 那是 schema 不是文件。這條界線是：
**問句可以反映資料裡有什麼，不可以反映 schema 怎麼描述自己。**

用法：
    python tools/make_data_cards.py                 # 印到 stdout
    python tools/make_data_cards.py --out cards.md
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.value_index import MYSQL_URI  # noqa: E402

LOW_CARD = 12          # 相異值 <= 這個數就列完整分布
SAMPLE_N = 3           # 高基數欄位給幾個樣本


def main(out_path: str | None) -> int:
    buf: list[str] = []
    w = buf.append
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        tables = [r[0] for r in conn.execute(text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() ORDER BY TABLE_NAME"))]
        fks = {}
        for t, c, rt, rc in conn.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME "
                "FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE "
                "WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL")):
            fks.setdefault(t, []).append(f"{c} → {rt}.{rc}")

        w(f"# 資料卡（{len(tables)} 張表）　※ 不含任何 schema 註解\n")
        for t in tables:
            n = conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar()
            cols = list(conn.execute(text(
                "SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t ORDER BY ORDINAL_POSITION"),
                {"t": t}))
            w(f"\n## {t}　（{n} 列）")
            if fks.get(t):
                w("外鍵：" + "；".join(fks[t]))
            for c, ctype, nullable in cols:
                nd = conn.execute(text(
                    f"SELECT COUNT(DISTINCT `{c}`) FROM `{t}`")).scalar()
                line = f"- `{c}` {ctype}{'' if nullable == 'NO' else ' NULL可'}　相異 {nd}"
                extra = ""
                if 0 < nd <= LOW_CARD:
                    dist = list(conn.execute(text(
                        f"SELECT `{c}`, COUNT(*) FROM `{t}` GROUP BY 1 "
                        f"ORDER BY 2 DESC, 1 LIMIT {LOW_CARD}")))
                    extra = "　分布 " + "、".join(
                        f"{v!r}×{k}" for v, k in dist)
                elif str(ctype).lower().startswith(("varchar", "char", "text")):
                    s = [r[0] for r in conn.execute(text(
                        f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL "
                        f"LIMIT {SAMPLE_N}"))]
                    if s:
                        extra = "　例 " + "、".join(repr(x)[:40] for x in s)
                w(line + extra)

    txt = "\n".join(buf)
    if out_path:
        io.open(out_path, "w", encoding="utf-8").write(txt)
        print(f"寫入 {out_path}　{len(txt):,} 字元、{len(txt.splitlines())} 行")
    else:
        print(txt)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    a = sys.argv
    sys.exit(main(a[a.index("--out") + 1] if "--out" in a else None))
