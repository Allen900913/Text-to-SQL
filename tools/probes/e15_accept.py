# -*- coding: utf-8 -*-
"""E15 驗收 —— 對事前登記的五條判準（見 tools/strip_column_enums.py 的 docstring）。

  ① 值索引・含代碼 14 題    GT 命中 >= 9      基線 0
  ② 值索引・其餘 291 題     GT 命中 >= 16     基線 16
  ③ 生成器不失血            0 例外            已在 strip_column_enums.py 驗過
  ④ 表檢索 Top-1            >= 244            基線 247
  ⑤ 欄位提示                >= 440            基線 442（E14）

④ 量的是 `briefs_for("retrieval")` —— enum 句已從 ddl 與 retrieval 投影拿掉，
所以檢索文件真的變了（catalog 保留 enum，不要拿它來量）。
"""
import io
import json
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "eval"), os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

from loguru import logger as log  # noqa: E402

from brief_arms import CACHE, load_cases, vectors  # noqa: E402
from langgraph_sql.utils.embedding import cosine  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.table_semantics import briefs_for  # noqa: E402
from langgraph_sql.utils.value_index import (VALUE_MAX_TABLES, get_value_index,  # noqa: E402
                                             value_hits)

_CODE = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b")
GOAL = {"①": 9, "②": 16, "④": 244}


def main() -> int:
    cases = load_cases(get_table_columns())
    code = [(i, q, n) for i, q, n in cases if _CODE.search(q)]
    rest = [(i, q, n) for i, q, n in cases if not _CODE.search(q)]
    idx = get_value_index()
    print(f"值索引 {len(idx)} 個值　VALUE_MAX_TABLES={VALUE_MAX_TABLES}\n")

    got = {}
    for lab, sub in (("①", code), ("②", rest)):
        hit = [i for i, q, n in sub if set().union(*n) & set(value_hits(q))]
        got[lab] = len(hit)
        print(f"[{lab}] {'含代碼' if lab == '①' else '其餘'} {len(sub)} 題　"
              f"GT 命中 {len(hit)}　門檻 >= {GOAL[lab]}　{'通過' if len(hit) >= GOAL[lab] else '✗ 不通過'}")
        if lab == "①":
            print(f"     命中的題：{hit}")
            print(f"     未命中：{[i for i, q, n in sub if i not in hit]}"
                  f"（#104 #159 是 expect: empty，本來就不該命中）")

    cache = json.load(io.open(CACHE, encoding="utf-8")) if os.path.exists(CACHE) else {}
    # ④ 要量**檢索**投影，不是 catalog —— `get_table_briefs()` 回的是
    # catalog（enum 保留），檢索文件走 briefs_for("retrieval")（enum 拿掉）。
    # 第一版量錯層，得到 247「恆等」，那是把沒動的那一份拿來當證據。
    A = vectors(briefs_for("retrieval"), cache)
    qv = cache["__queries__"]
    top1 = sum(1 for (_, _, nd), v in zip(cases, qv)
               if any(min(A, key=lambda t: (-cosine(v, A[t]), t)) in n for n in nd))
    got["④"] = top1
    print(f"\n[④] 表檢索 Top-1 {top1}/{len(cases)}　門檻 >= {GOAL['④']}　"
          f"{'通過' if top1 >= GOAL['④'] else '✗ 不通過'}"
          f"（A 基線 247）")

    ok = all(got[k] >= GOAL[k] for k in GOAL)
    print("\n" + "=" * 56)
    print("①②④ 全過。⑤ 由 hint_strip.py 量（E14 已得 442 >= 440）。" if ok else "有判準沒過。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
