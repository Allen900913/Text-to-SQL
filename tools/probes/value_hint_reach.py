# -*- coding: utf-8 -*-
"""實驗一的射程與對照組 —— 零 LLM，跑在上一輪存下來的檢索結果上。

為什麼能離線算
====================================================================
給定 `retrieved_tables`，兩臂的 System Prompt 差異是**確定的**：
A 臂 `value_block` 是空字串，B 臂多一個區塊。所以拿上一輪開發集的
309 組檢索結果，就能在不打任何 API 的情況下算出：

    · 幾題會拿到值命中區塊（= 射程）
    · 沒拿到的那些題，兩臂 Prompt 是不是真的逐位元相同（= 對照組）
    · 單邊化幾次（值的另一個歸屬被選表砍掉）

⚠️ 這**不是**真跑的對照組。真跑時檢索自己會抖（20.5%，
[[retrieval-is-nondeterministic]]），所以正式驗收看的是結果 JSON 裡的
`prompt_hash`，不是這一支。這一支只回答「值不值得跑」。

用法：python tools/probes/value_hint_reach.py [結果檔]
"""
import hashlib
import io
import json
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for p in (_ROOT, os.path.join(_ROOT, "eval")):
    if p not in sys.path:
        sys.path.insert(0, p)

os.environ["VALUE_HINT"] = "1"          # 探針自己開，production 預設仍是 0

from loguru import logger as log  # noqa: E402

log.remove()

from langgraph_sql.nodes.sql_generator import _build_system_prompt  # noqa: E402
from langgraph_sql.utils.value_index import value_locations  # noqa: E402

SRC = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
    _ROOT, "eval", "results", "eval_result_1789186397.json")


def main() -> int:
    d = json.load(io.open(SRC, encoding="utf-8"))
    rows = d.get("results") if isinstance(d, dict) else d
    hit, same, diff, single = [], 0, 0, 0
    for r in rows:
        tabs = r.get("retrieved_tables") or []
        txt, ss = value_locations(r["question"], tabs)
        base = {"schema_ddl": "<DDL>", "enum_text": "<E>", "rules_text": "<R>"}
        a = _build_system_prompt(dict(base, value_hint_text=""))
        b = _build_system_prompt(dict(base, value_hint_text=txt))
        if txt:
            hit.append((r["id"], r["question"], txt, ss, r["outcome"]))
            single += ss
            diff += 1
        else:
            same += hashlib.md5(a.encode()).digest() == hashlib.md5(b.encode()).digest()
    n = len(rows)
    print("來源：%s（%d 題）\n" % (os.path.basename(SRC), n))
    print("射程    %d 題拿到值命中區塊 = %.1f%%" % (len(hit), 100.0 * len(hit) / n))
    print("對照組  %d 題兩臂 Prompt 逐位元相同 = %.1f%%" % (same, 100.0 * same / n))
    print("        （%d + %d = %d，應等於 %d）" % (len(hit), same, len(hit) + same, n))
    print("單邊化  %d 次 —— 值的另一個歸屬被選表砍掉，生成器只看得到單邊" % single)

    print("\n逐題（★ = 該值住在多個地方，歧義攤在同一行上）")
    for qid, q, txt, ss, oc in hit:
        multi = any("," in ln for ln in txt.split("\n"))
        print("  %s #%-5s %-34s %s" % ("★" if multi else " ", qid, q[:32],
                                       "單邊%d" % ss if ss else ""))
        for ln in txt.split("\n"):
            print("        %s" % ln)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
