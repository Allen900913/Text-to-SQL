# -*- coding: utf-8 -*-
"""E15：把代碼字面移出欄位註解，改由 enum_fields 送給生成器。

做三件事，缺一不可：
  ① 欄位註解改寫（`strip_enum_codes.strip`），中文說明留下
  ② 該欄的 `enums` 宣告把 `arm: dup` 拿掉 —— dup 的意思是「值已在註解裡，
     不必再送」，註解裡沒有了就必須送，否則生成器直接失血
  ③ 驗收：每個被改寫欄位的**每一個值**都要出現在 `enum_text()` 裡

只改 YAML，不碰資料庫。套用要另外跑 `tools/sync_table_comments.py --apply`。
`--revert` 從 git 還原 YAML。

事前登記的判準（2026-09-09，看完基線、跑這支之前寫）：
  ① 值索引・含代碼 14 題    GT 命中 >= 9      基線 0（模擬 11、天花板 12）
  ② 值索引・其餘 291 題     GT 命中 >= 16     基線 16
  ③ 生成器不失血            0 例外            —
  ④ 表檢索 Top-1            >= 244            基線 247
  ⑤ 欄位提示                >= 440            基線 442
"""
import io
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.value_index import MYSQL_URI  # noqa: E402
from strip_enum_codes import should_strip, strip  # noqa: E402

PATH = os.path.join(_ROOT, "utils", "table_semantics.yaml")


def main() -> int:
    d = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        cols = conn.execute(text(
            "SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE IN ('varchar','char','enum')")).fetchall()
        V = {}
        for t, c in cols:
            V[(t, c)] = {str(v).strip() for (v,) in conn.execute(
                text(f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL"))}

    done, undeclared, missing_vals = [], [], []
    for t, spec in d["tables"].items():
        for c, cm in list((spec.get("columns") or {}).items()):
            vals = V.get((t, c), set())
            if not should_strip(t, c, cm or "", vals):
                continue
            spec["columns"][c] = strip(cm, vals)
            e = (spec.get("enums") or {}).get(c)
            if e is None:
                undeclared.append(f"{t}.{c}")
            else:
                e.pop("arm", None)                    # dup → 送出
                gap = vals - set(e.get("values") or {})
                if gap:
                    missing_vals.append((f"{t}.{c}", sorted(gap)))
            done.append(f"{t}.{c}")

    print(f"改寫 {len(done)} 個欄位註解")
    if undeclared:
        print(f"✗ 沒有 enums 宣告的 {len(undeclared)} 個：{undeclared}")
    if missing_vals:
        print(f"✗ 宣告漏了資料裡的值 {len(missing_vals)} 個：{missing_vals}")
    if undeclared or missing_vals:
        print("\n未寫入 —— 先補齊宣告，否則生成器會失血（判準 ③）。")
        return 1

    io.open(PATH, "w", encoding="utf-8").write(yaml.safe_dump(
        d, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096))

    # 判準 ③：每個被改寫欄位的每個值，都要出現在 enum_text() 裡
    et = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,r'%s');from loguru import logger as l;l.remove();"
         "from langgraph_sql.utils.table_semantics import enum_text;"
         "sys.stdout.reconfigure(encoding='utf-8');print(enum_text(),end='')" % _ROOT],
        capture_output=True, text=True, encoding="utf-8").stdout
    bad = []
    for f in done:
        t, _, c = f.partition(".")
        for v in sorted(V.get((t, c), set())):
            if f"'{v}'" not in et:
                bad.append(f"{f} = {v}")
    print(f"\n[判準 ③] enum_text() {len(et)} 字元；"
          f"被改寫欄位的值缺漏 {len(bad)} 個{'' if not bad else '：' + str(bad[:12])}")
    print("YAML 已寫入。接著跑 tools/sync_table_comments.py --apply 才會進資料庫。")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    if "--revert" in sys.argv:
        subprocess.run(["git", "checkout", "--", "utils/table_semantics.yaml"], cwd=_ROOT)
        print("YAML 已從 git 還原")
        sys.exit(0)
    sys.exit(main())
