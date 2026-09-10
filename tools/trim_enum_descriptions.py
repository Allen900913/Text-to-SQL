# -*- coding: utf-8 -*-
"""`enums` 宣告的 description 重複列出自己的值 —— 砍掉那一段。

為什麼會有這個
================================================================
值域宣告是從舊的欄位註解搬過來的，description 直接抄了註解的前半句，
於是生成器看到的是同一組代碼列兩次：

    customer_profiles.gender (性別 (M/F/OTHER))
      - 'F'
      - 'M'
      - 'OTHER'

這不影響值索引（`_keep` 讀的是資料庫 TABLE_COMMENT／COLUMN_COMMENT，
description 從來不進那裡），純粹是生成器 Prompt 的冗字。

規則要保守，因為列舉裡常常夾著真的語意
================================================================
只砍**尾端**那一段、而且那一段裡的每一個 token 都是已宣告的值。
所以這些會被砍：

    性別 (M/F/OTHER)                  → 性別
    鎖定裝置：ALL／MOBILE／DESKTOP     → 鎖定裝置

這些**不會**被砍，因為列舉之外還有別的意思：

    職級：J1~J8，數字越大越資深       ← 「數字越大越資深」是排序語意
    英語能力等級：A1~C2               ← 區間記法，不是列舉
    危險品分類，NONE 表示非危險品      ← 「表示非危險品」是 NONE 的說明
                                        （它已經在 values 裡，另外處理）

砍完 description 不能變空 —— 空的說明比重複的說明更糟。

用法：
    python tools/trim_enum_descriptions.py            # 預覽
    python tools/trim_enum_descriptions.py --write
"""
import io
import os
import re
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402

PATH = os.path.join(_ROOT, "utils", "table_semantics.yaml")
_SEP = r"[／/、,，]"


def trim(desc: str, values) -> str:
    """砍掉尾端「只列出已宣告的值」的那一段。砍不動就原樣回傳。"""
    vals = {str(v) for v in values}
    for pat in (r"\s*[（(]\s*(?P<body>[^（()）]*?)\s*[）)]\s*$",   # 尾端括號
                r"\s*[：:]\s*(?P<body>[^：:]*?)\s*$"):              # 尾端冒號
        m = re.search(pat, desc)
        if not m:
            continue
        toks = [t.strip() for t in re.split(_SEP, m.group("body")) if t.strip()]
        # 每一個 token 都要是已宣告的值，而且至少要有兩個 —— 只有一個時
        # 多半是「NONE 表示非危險品」這種說明，不是列舉
        if len(toks) < 2 or any(t not in vals for t in toks):
            continue
        out = desc[:m.start()].rstrip("　 ,，、：:")
        if out:
            return out
    return desc


def main(write: bool) -> int:
    d = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    hits = []
    for t, spec in d["tables"].items():
        for c, e in (spec.get("enums") or {}).items():
            old = e.get("description") or ""
            new = trim(old, (e.get("values") or {}))
            if new != old:
                hits.append((f"{t}.{c}", old, new))
                if write:
                    e["description"] = new
    for k, old, new in hits:
        print(f"  {k:48} {old}  →  {new}")
    print(f"\n{len(hits)} 個 description 砍掉重複的值列")
    if not write:
        print("預覽而已，加 --write 才會寫。")
        return 0
    io.open(PATH, "w", encoding="utf-8").write(yaml.safe_dump(
        d, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096))
    et = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,r'%s');from loguru import logger as l;l.remove();"
         "from langgraph_sql.utils.table_semantics import enum_text;"
         "sys.stdout.reconfigure(encoding='utf-8');print(len(enum_text()))" % _ROOT],
        capture_output=True, text=True, encoding="utf-8").stdout.strip()
    print(f"YAML 已寫入。enum_text() 現在 {et} 字元。"
          "（description 不進資料庫註解，不必跑 sync）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main("--write" in sys.argv))
