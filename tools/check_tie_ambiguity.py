# -*- coding: utf-8 -*-
"""閘門 [14] 並列歧義 —— 「前 N 名」的第 N 名有沒有並列。

問題長什麼樣
================================================================
「哪五樣東西最常被申請保固？各幾次？」保固次數是 3、2、2、2、2、2……
第 5 名跟第 6 名都是 2 次。**問句決定不了該挑哪一個**，系統挑了
「行動電源」、GT 挑了「快煮壺」，判分要求列數相符且涵蓋每一個值 ——
永遠答不對，量到的是雜訊不是能力。

這是閘門 [13]／[6] 的第三條軸：
    [13] 問句決定不了要**哪幾欄**
    [6]  問句決定不了要算**哪些列**
    [14] 問句決定不了**同分的時候誰排前面**

為什麼上一版的偵測器測不到
================================================================
上一版用「ORDER BY 後面接 RAND(seed) 跑兩次」，掃 v3 掃出 0 題。
看起來很乾淨，其實是**我自己把它弄瞎的**：

    出題的時候為了讓 GT 有唯一答案，我在 ORDER BY 後面加了 tiebreak
        ORDER BY n DESC, p.name LIMIT 5
    加完之後 RAND 再也改變不了任何東西 —— GT 決定了順序，
    而**問句沒有**。偵測器測的是 GT，不是問句。

所以這一版只保留**第一個** ORDER BY 項（問句真正指定的那個排序依據），
把後面我自己補的 tiebreak 全部丟掉，再接 RAND。測的是問句，不是 GT。

怎麼判
================================================================
同一句 SQL、同一個主排序鍵、兩個不同的 RAND 種子跑兩次。
答案會變就是並列沒定。不變的不報 —— 沒有並列就沒有暴露
（閘門量的是暴露，不是待辦清單）。

用法
    python tools/check_tie_ambiguity.py eval/validation_style.yaml
    python tools/check_tie_ambiguity.py            # 預設掃開發集
"""
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "eval")):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml

from eval_score import match_ordered, match_unordered, to_rows
from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager

# 問句自己講明白同分怎麼處理的字樣。
# ⚠️ 豁免會被驗證：問句說了「同分的按品名排」而 GT 的 ORDER BY 只有一項，
#    照樣報 —— 那句話是空的，排序依然沒有決定。豁免不能只憑問句說了就放行。
SETTLED = ("並列", "同分", "任一", "任選", "隨便", "都可以", "其中一")

# 逐題裁決：問句的**主題**就是那個排序，並列不影響答案。
ADJUDICATED: dict[int, str] = {}

# 用 8 顆種子而不是 2 顆。只有兩個並列項時，兩顆種子有 1/2 機率排出相同順序；
# 8 顆把漏報率壓到 2^-7 以下。零 LLM，成本只是多跑幾次 SQL。
SEEDS = (11, 97, 3, 5077, 41, 613, 829, 7)


def outer_order_by(sql: str) -> int:
    """最外層 ORDER BY 的位置；沒有或在子查詢裡就回 -1。"""
    i = sql.upper().rfind("ORDER BY")
    if i < 0:
        return -1
    tail = sql[i:]
    return i if tail.count(")") <= tail.count("(") + 1 else -1


def n_order_terms(sql: str) -> int:
    """最外層 ORDER BY 有幾個排序項 —— 用來驗「問句說的 tiebreak 有沒有實作」。"""
    i = outer_order_by(sql)
    if i < 0:
        return 0
    m = re.search(r"\bLIMIT\b", sql[i:], re.I)
    cut = i + m.start() if m else len(sql)
    depth, terms = 0, 1
    for ch in sql[i + len("ORDER BY"):cut]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == "," and depth == 0:
            terms += 1
    return terms


def primary_key_only(sql: str, seed: int) -> str:
    """只留第一個排序項，後面的 tiebreak 丟掉，再接 RAND(seed)。

    丟掉 tiebreak 是這支的重點：那是出題的人補的，問句裡沒有。
    留著它，這個偵測器就永遠是綠的。
    """
    i = outer_order_by(sql)
    if i < 0:
        return ""
    m = re.search(r"\bLIMIT\b", sql[i:], re.I)
    cut = i + m.start() if m else len(sql)
    terms, depth, cur = [], 0, ""
    for ch in sql[i + len("ORDER BY"):cut]:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            terms.append(cur)
            cur = ""
        else:
            cur += ch
    terms.append(cur)
    first = terms[0].strip()
    if not first:
        return ""
    return "%sORDER BY %s, RAND(%d) %s" % (sql[:i], first, seed, sql[cut:])


def scan(entries, db):
    """回傳 (可量測題數, 有排序的題數, 命中清單, 已裁決命中)。"""
    hits, adj, n, ordered_n = [], [], 0, 0
    for e in entries:
        sql = e.get("sql")
        if not sql or e.get("expect") == "schema_unsupported":
            continue
        n += 1
        if outer_order_by(sql) < 0:
            continue
        ordered_n += 1
        if any(w in e["question"] for w in SETTLED):
            # 豁免要驗，不能只憑問句說了就放行。問句寫「同分的按品名排」而
            # GT 的 ORDER BY 只有一項，那句話是空的 —— 排序依然沒有決定。
            if n_order_terms(sql) < 2:
                hits.append((e["id"], 0, "問句說了同分怎麼排，GT 的 ORDER BY 卻只有一項"))
            continue
        # 種子要多。只有兩個並列項的時候，兩個種子有一半機率排出一樣的順序 ——
        # #3010（兩把 893 次的快取鍵）就是這樣從兩顆種子底下溜掉的。
        m = match_ordered if e.get("ordered") else match_unordered
        base, changed = None, False
        for seed in SEEDS:
            alt = primary_key_only(sql, seed)
            if not alt:
                break
            try:
                rows = to_rows(db.execute_to_dataframe(alt))
            except Exception:
                break
            if base is None:
                base = rows
            elif not m(rows, base):
                changed = True
                break
        if changed:
            (adj if e["id"] in ADJUDICATED else hits).append(
                (e["id"], len(base), "順序" if e.get("ordered") else "前 N 名的集合"))
    return n, ordered_n, hits, adj


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = args[0] if args else os.path.join(_ROOT, "eval_ground_truth.yaml")
    if not os.path.isabs(path):
        path = os.path.join(_ROOT, path)
    entries = yaml.safe_load(io.open(path, encoding="utf-8"))
    db = get_db_manager(MYSQL_URI)
    n, ordered_n, hits, adj = scan(entries, db)

    print("題庫：%s" % os.path.relpath(path, _ROOT))
    print("閘門 [14] 並列歧義 —— %d 題可量測，其中 %d 題有排序\n" % (n, ordered_n))
    if hits:
        print("命中 %d 題（占有排序的 %.1f%%）—— 同分的時候問句決定不了誰排前面："
              % (len(hits), 100.0 * len(hits) / ordered_n if ordered_n else 0))
        for qid, rows, what in hits:
            print("   #%-6s %s" % (qid, what if not rows
                                   else "%d 列　換個種子%s就變" % (rows, what)))
        print("\n改法：問句補上 tiebreak 的依據（「同分的按名稱排」），或改問不會並列的東西。")
        print("**在 GT 的 ORDER BY 補 tiebreak 不算修好** —— 那只是把偵測器弄瞎，")
        print("問句還是決定不了，系統還是會挑另一個。")
    if adj:
        print("\nℹ️ 已裁決 %d 題：" % len(adj))
        for qid, _, _ in adj:
            print("   #%-6s %s" % (qid, ADJUDICATED[qid]))
    if not hits:
        print("閘門 [14] 綠燈。")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
