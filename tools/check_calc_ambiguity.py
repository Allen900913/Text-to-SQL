# -*- coding: utf-8 -*-
"""閘門 [15] 計算方式歧義 —— 同一批列，算法不同答案不同。

第四條軸
================================================================
    [13] 問句決定不了要**哪幾欄**
    [6]  問句決定不了要算**哪些列**
    [14] 問句決定不了**同分的時候誰排前面**
    [15] 問句決定不了**怎麼算**（要不要去重、NULL 算不算）

兩種替代算法
    [D] 去重　COUNT(x) → COUNT(DISTINCT x)
              「這些訂單用了幾種付款方式」到底是筆數還是種類數
    [N] 空值　AVG(x) → AVG(COALESCE(x,0))、COUNT(x) → COUNT(*)
              沒填的那些算 0 還是不算

為什麼要防誤報（上一版的教訓）
================================================================
原型版的 [N] 掃形狀驗證集掃出 **45.3%** 命中，看起來災難，其實全是誤報：
那組題是 LEFT JOIN 的零列群組題，`COUNT(o.id)` 換成 `COUNT(*)` 當然
0 變 1 —— 但那正是那些題要測的東西，問句用「一張都沒下過的也算 0」
講明白了。**偵測器把題目的主題當成缺陷。**

所以 [N] 的 COUNT(x)→COUNT(*) 這一半碰到 LEFT JOIN 就跳過。
AVG/SUM 的 COALESCE 那一半不受影響（LEFT JOIN 不會製造出「該不該補 0」
的歧義，那是欄位本身可不可空的問題）。

閘門量的是暴露不是缺陷 —— 答案不變的一律不報。

用法
    python tools/check_calc_ambiguity.py eval/testset_holdout.yaml
    python tools/check_calc_ambiguity.py            # 預設掃開發集
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

# 問句已經講明算法的字樣。
SETTLED_DISTINCT = ("不重複", "幾種", "個不同", "位不同", "樣不同", "去重", "不同的",
                    "種類", "幾類")
SETTLED_NULL = ("算 0", "算0", "當 0", "當0", "沒填的", "空白的", "沒有填",
                "含空值", "不含空值", "都算")

ADJUDICATED: dict[int, str] = {
    # COUNT(answer)*100/COUNT(*) 是個比率，分子分母都是**筆數**。
    # 去重成「幾種不同的回覆文字」在中文裡不是一個站得住的讀法 ——
    # 這是 distinctify 無差別包住每一個 COUNT 造成的誤報，不是題目的缺陷。
    # 不寫成規則（「比率就跳過」），因為分子是不是該去重要看題目在問什麼。
    225: "「回覆率」的分子分母都是筆數，去重沒有意義",
}


def distinctify(sql: str) -> str:
    out, n = re.subn(r"\bCOUNT\(\s*(?!DISTINCT|\*)", "COUNT(DISTINCT ", sql, flags=re.I)
    return out if n else ""


def coalesced(sql: str) -> str:
    out, n = re.subn(r"\b(AVG|SUM)\(\s*(?!DISTINCT)([A-Za-z_]\w*(?:\.\w+)?)\s*\)",
                     r"\1(COALESCE(\2,0))", sql, flags=re.I)
    return out if n else ""


def countstar(sql: str) -> str:
    """COUNT(欄位) → COUNT(*)。有 LEFT JOIN 就不做 —— 那不是歧義，是題目的主題。"""
    if re.search(r"\bLEFT\s+(OUTER\s+)?JOIN\b", sql, re.I):
        return ""
    out, n = re.subn(r"\bCOUNT\(\s*(?!DISTINCT|\*)([A-Za-z_]\w*(?:\.\w+)?)\s*\)",
                     "COUNT(*)", sql, flags=re.I)
    return out if n else ""


def scan(entries, db):
    hits, adj, n = [], [], 0
    for e in entries:
        sql = e.get("sql")
        if not sql or e.get("expect") == "schema_unsupported":
            continue
        n += 1
        try:
            gt = to_rows(db.execute_to_dataframe(sql))
        except Exception:
            continue
        m = match_ordered if e.get("ordered") else match_unordered
        q = e["question"]

        def changed(alt):
            if not alt:
                return False
            try:
                return not m(to_rows(db.execute_to_dataframe(alt)), gt)
            except Exception:
                return False

        why = ""
        if not any(w in q for w in SETTLED_DISTINCT) and changed(distinctify(sql)):
            why = "[D] 去重　COUNT 要不要 DISTINCT，答案就變"
        elif not any(w in q for w in SETTLED_NULL) and (
                changed(coalesced(sql)) or changed(countstar(sql))):
            why = "[N] 空值　NULL 算不算進來，答案就變"
        if why:
            (adj if e["id"] in ADJUDICATED else hits).append((e["id"], why))
    return n, hits, adj


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = args[0] if args else os.path.join(_ROOT, "eval_ground_truth.yaml")
    if not os.path.isabs(path):
        path = os.path.join(_ROOT, path)
    entries = yaml.safe_load(io.open(path, encoding="utf-8"))
    db = get_db_manager(MYSQL_URI)
    n, hits, adj = scan(entries, db)

    print("題庫：%s" % os.path.relpath(path, _ROOT))
    print("閘門 [15] 計算方式歧義 —— %d 題可量測\n" % n)
    if hits:
        print("命中 %d 題（%.1f%%）—— 問句決定不了怎麼算：" % (len(hits), 100.0 * len(hits) / n))
        for qid, why in hits:
            print("   #%-6s %s" % (qid, why))
        print("\n改法：問句講明算法（「幾種不同的」／「沒填的算 0」），**GT 不動**。")
    if adj:
        print("\nℹ️ 已裁決 %d 題：" % len(adj))
        for qid, _ in adj:
            print("   #%-6s %s" % (qid, ADJUDICATED[qid]))
    if not hits:
        print("閘門 [15] 綠燈。")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
