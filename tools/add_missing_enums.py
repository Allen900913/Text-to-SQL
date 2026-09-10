# -*- coding: utf-8 -*-
"""補上 8 個「欄位註解列了值域、但 enums 沒有宣告」的欄位。標 arm: dup。

為什麼要補
====================================================================
`arm: dup` 的意思是「值已經寫在欄位註解裡，DDL 就帶著了，`enum_text` 不再送
一次」。所以補宣告是**位元中性**的 —— `enum_text()` 的輸出逐字不變。
它的作用有兩個：

  ① 閘門 [4b]（值域新鮮度）只驗宣告過的欄位。漏宣告的欄位跟漏登記的
     來源一樣，**不可能被檢查到** —— 這正是 `wide_table_plan_2.yaml`
     那次的教訓（未登記的來源不可能不一致）。
  ② 代碼要離開欄位註解時，這 8 欄才有東西接手：把 `arm: dup` 拿掉，
     值就自動改由 `enum_text` 送給生成器。沒有宣告就是直接失血。

值只宣告**資料裡真的有的**。註解上多列的（`applies_to_scope` 的 SKU、
`applies_to` 的 PRODUCT）不補進來 —— 那是註解的死代碼，是另一回事，
而且閘門 [4b] 本來就在數它。
"""
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.table_semantics import enum_text  # noqa: E402
from langgraph_sql.utils.value_index import MYSQL_URI  # noqa: E402

PATH = os.path.join(_ROOT, "utils", "table_semantics.yaml")
TARGET = ["campaign_profiles.placement", "campaigns.channel",
          "promotion_profiles.applies_to_scope", "promotion_profiles.display_slot",
          "promotion_profiles.review_cycle", "promotion_rules.applies_to",
          "store_profiles.district_type", "supplier_profiles.payment_terms"]

# 「FEED 動態」這種「代碼＋中文註釋」的配對，中文那半就是值的說明。
_PAIR = re.compile(r"\b([A-Z][A-Z0-9_]{2,})\s*([一-鿿0-9]{0,8})")


def main() -> int:
    before = enum_text()
    d = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    added = []
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        for f in TARGET:
            t, c = f.split(".")
            spec = d["tables"][t]
            cm = (spec.get("columns") or {}).get(c) or ""
            vals = sorted({str(v).strip() for (v,) in conn.execute(
                text(f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL"))})
            gloss = dict(_PAIR.findall(cm))
            spec.setdefault("enums", {})[c] = {
                "description": cm.split("：")[0].split("(")[0].strip(),
                "arm": "dup",
                "values": {v: gloss.get(v, "") for v in vals},
            }
            added.append((f, vals, [v for v in vals if gloss.get(v)]))

    io.open(PATH, "w", encoding="utf-8").write(yaml.safe_dump(
        d, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096))

    print(f"{'欄位':<40}{'值':<44}有中文說明")
    print("-" * 100)
    for f, vals, g in added:
        print(f"{f:<40}{','.join(vals)[:42]:<44}{len(g)}/{len(vals)}")

    # 位元中性驗收：要重讀 YAML，所以在子行程裡比
    import subprocess
    after = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,r'%s');"
         "from loguru import logger as l;l.remove();"
         "from langgraph_sql.utils.table_semantics import enum_text;"
         "sys.stdout.reconfigure(encoding='utf-8');print(enum_text(),end='')" % _ROOT],
        capture_output=True, text=True, encoding="utf-8").stdout
    ok = after == before
    print(f"\n[驗收] enum_text() 逐位元{'相同' if ok else '**不同**'}"
          f"（{len(before)} → {len(after)} 字元）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
