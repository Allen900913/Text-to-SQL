# -*- coding: utf-8 -*-
"""閘門 [6] 旗標歧義 —— 問句有沒有講明母體。

問題長什麼樣
================================================================
「給滿分五分的評論總共有幾則？」GT 算 46，系統算 44 —— 系統扣掉了
is_deleted=1 的兩則。兩種讀法都站得住，問句自己決定不了。那種題
**永遠答不對**，量到的是雜訊不是能力。

這跟閘門 [13]（問句決定不了 SELECT 清單）是同一類缺陷的另一個維度：
[13] 管的是「要哪幾欄」，這支管的是「算哪些列」。

怎麼判
================================================================
不用字面規則猜，直接**把另一種讀法跑一次**：把 GT 碰到的表換成套了旗標的
子查詢，重跑，答案會變就是歧義。答案不變的不報 —— 那題無論怎麼讀都一樣，
沒有暴露（閘門量的是暴露，不是待辦清單）。

兩種豁免
    SETTLED     問句自己講明白了（「含已刪除的」「在職離職都算」）
    ADJUDICATED 問句的**主題**就是那個旗標（「哪幾位同事已經離開公司了？」）
                —— 這種要逐題裁決，理由寫在下面，不寫成規則。

用法
    python tools/check_flag_ambiguity.py eval/testset_holdout.yaml
    python tools/check_flag_ambiguity.py            # 預設掃開發集
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

# 「自然的另一種讀法」—— 一般人問「幾則評論」時多半不含已刪除的，
# 問「每位同仁」時多半指在職的。只列窄表；寬表 _profiles 的旗標是
# 題目的主題不是預設過濾（問「有幾個 VIP」沒有人會預設排除非 VIP）。
ALT_READING = {
    "reviews": "is_deleted = 0",
    "employees": "is_active = 1",
    "stores": "is_active = 1",
    "warehouses": "is_active = 1",
    "categories": "is_active = 1",
    "payment_methods": "is_active = 1",
    "promotions": "is_active = 1",
    "carts": "is_abandoned = 0",
    "invoices": "is_voided = 0",
    "newsletter_subscriptions": "unsubscribed_at IS NULL",
    "subscriptions": "ended_at IS NULL",
    "service_appointments": "attended = 1",
    "product_categories": "is_primary = 1",
}

# 問句已經自己講明白的字樣。
SETTLED = ("已刪除", "沒被刪", "含刪除", "在職", "離職", "全部同仁", "所有同仁",
           "停用", "啟用", "有效", "未取消", "已取消", "含已", "不含", "只算",
           "已棄置", "棄置", "作廢", "退訂", "實際到場", "沒到場", "主分類",
           "已停業", "都算",
           # #254「有多少筆到店服務預約客戶沒有出席？」—— 問句自己講明了，
           # 卻因為清單裡只有「到場」沒有「出席」而被報成歧義。同義詞漏一個，
           # 閘門就會把講清楚的題目當成沒講清楚。
           "出席")

# 逐題裁決：問句的主題就是那個旗標，不是預設過濾。
# 這種不能寫成規則 —— 規則會把「哪幾位同事離職了」跟「每個職稱幾個人」
# 當成同一件事，而它們正好相反。
ADJUDICATED = {
    1002: "「已經離開公司了」問的就是 is_active=0，旗標是主題",
    1003: "「目前停止營運的門市」問的就是 is_active=0",
    1017: "「被刪掉的評價佔全部的幾成」分子分母都講明了",
    1018: "「丟著沒結帳的購物車」問的就是 is_abandoned=1",
    1107: "「從來沒把東西放進購物車過」—— 棄置的購物車也是放過東西",
    131: "「丟著沒結帳的比例」旗標是主題",
    132: "「丟著沒結帳的比例」旗標是主題",
    162: "「作廢發票」旗標是主題",
}


def with_alt(sql: str, table: str, cond: str) -> str:
    """把 FROM/JOIN 到的那張表換成套了旗標的子查詢。

    不改寫 WHERE —— WHERE 裡的條件可能引用別的別名。換掉來源表最安全：
    別名保持原樣，外層一個字都不用動。
    """
    pat = re.compile(
        r"\b(FROM|JOIN)\s+%s\b(\s+(?!ON\b|WHERE\b|GROUP\b|JOIN\b|LEFT\b|LIMIT\b|ORDER\b|HAVING\b)([A-Za-z_]\w*))?"
        % re.escape(table), re.I)

    def rep(m):
        alias = m.group(3) or table
        return "%s (SELECT * FROM %s WHERE %s) %s" % (m.group(1), table, cond, alias)

    out, n = pat.subn(rep, sql)
    return out if n else ""


def scan(entries, db):
    """回傳 (可量測題數, [(id, table, col, gt列, alt列)], 已裁決命中的 id)。"""
    hits, adj, n = [], [], 0
    for e in entries:
        if e.get("expect") == "schema_unsupported" or not e.get("sql"):
            continue
        n += 1
        if any(w in e["question"] for w in SETTLED):
            continue
        try:
            df = db.execute_to_dataframe(e["sql"])
        except Exception:
            continue
        gt = to_rows(df)
        for table, cond in ALT_READING.items():
            alt = with_alt(e["sql"], table, cond)
            if not alt:
                continue
            try:
                adf = db.execute_to_dataframe(alt)
            except Exception:
                continue
            m = match_ordered if e.get("ordered") else match_unordered
            if not m(to_rows(adf), gt):
                if e["id"] in ADJUDICATED:
                    adj.append(e["id"])
                else:
                    hits.append((e["id"], table, cond.split()[0], len(df), len(adf)))
                break
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
    print("閘門 [6] 旗標歧義 —— %d 題可量測\n" % n)
    if hits:
        print("命中 %d 題（%.1f%%）—— 問句決定不了要算哪些列：" % (len(hits), 100.0 * len(hits) / n))
        for qid, t, c, a, b in hits:
            print("   #%-6s %s.%s　GT %d 列 / 另一讀法 %d 列" % (qid, t, c, a, b))
        print("\n改法：問句寫明母體（「含已刪除的」「在職離職都算」），**GT 不動**。")
        print("反過來改 GT 去迎合系統的讀法，就是看著結果挑答案。")
    if adj:
        print("\nℹ️ 已裁決 %d 題（旗標是題目的主題，不是預設過濾）：" % len(adj))
        for qid in adj:
            print("   #%-6s %s" % (qid, ADJUDICATED[qid]))
    if not hits:
        print("\n閘門 [6] 綠燈。")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
