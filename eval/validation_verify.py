# -*- coding: utf-8 -*-
"""形狀驗證集的閘門 —— 核心是「鑑別力」。

這組題存在的理由是分辨兩種寫法。所以最重要的檢查不是「GT 跑得動」，
而是「天真寫法會被判錯」：

    用**判分器本人**（eval_score.judge 的比對函式）拿 trap_sql 去對 sql，
    判成 correct 的那題就是啞彈 —— 系統答對只代表兩種寫法都對，量不到東西。

其餘檢查：
    [1] sql 可執行且非空（空集合題量不到聚合形狀）
    [2] trap_sql 可執行
    [3] 鑑別力：trap_sql 對 sql 必須判錯
    [4] 不與開發集 / 驗收集重複（字元三元組 Jaccard）
    [5] 列數上限（判分要比多重集合，太大就只是在測 LIMIT）
    [6] 旗標歧義：GT 碰到的表若有 is_deleted / is_active 這類旗標，把「自然的
        另一種讀法」套上去重跑一次；答案會變就是問句沒說清楚。

        這道是被 #2211 逼出來的。「給滿分五分的評論總共有幾則？」GT 算 46，
        系統算 44（扣掉 is_deleted=1 的兩則）—— 系統的讀法完全站得住，
        是問句自己決定不了。那種題永遠答不對，量到的是雜訊不是能力。
        跟建驗收集時抓到的「統計快照 vs 逐張現算」是同一類缺陷。

用法：
    python eval/validation_verify.py
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml

from eval_score import match_ordered, match_unordered, to_rows
from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager

PATH = os.path.join(_ROOT, "eval", "validation_shapes.yaml")
DEV = os.path.join(_ROOT, "eval_ground_truth.yaml")
HOLDOUT = os.path.join(_ROOT, "eval", "testset_holdout.yaml")
ROW_MAX = 220
DUP_MAX = 0.62

# 「自然的另一種讀法」—— 一般人問「幾則評論」時多半不含已刪除的，
# 問「每位同仁」時多半指在職的。這裡只列窄表；寬表 _profiles 的旗標
# 是題目的主題不是預設過濾，不算歧義。
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

# 問句已經自己講明白的字樣 —— 講明白了就不算歧義。
SETTLED = ("已刪除", "沒被刪", "含刪除", "在職", "離職", "全部同仁", "所有同仁",
           "停用", "啟用", "有效", "未取消", "已取消", "含已", "不含", "只算",
           "已棄置", "棄置", "作廢", "退訂", "實際到場", "沒到場", "主分類")


def with_alt(sql: str, table: str, cond: str) -> str:
    """把 FROM/JOIN 到的那張表換成套了旗標的子查詢。

    不改寫 WHERE，因為 WHERE 裡的條件可能引用別的別名；換掉來源表最安全，
    別名保持原樣，外層一個字都不用動。
    """
    import re as _re
    pat = _re.compile(r"\b(FROM|JOIN)\s+%s\b(\s+(?!ON\b|WHERE\b|GROUP\b|JOIN\b|LEFT\b|LIMIT\b|ORDER\b)([A-Za-z_]\w*))?"
                      % _re.escape(table), _re.I)

    def rep(m):
        alias = m.group(3) or table
        return "%s (SELECT * FROM %s WHERE %s) %s" % (m.group(1), table, cond, alias)

    out, n = pat.subn(rep, sql)
    return out if n else ""


def tri(s: str) -> set:
    s = "".join(s.split())
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def main() -> int:
    entries = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    db = get_db_manager(MYSQL_URI)
    bad = {1: [], 2: [], 3: [], 4: [], 5: [], 6: []}

    others = []
    for p in (DEV, HOLDOUT):
        if os.path.exists(p):
            for e in yaml.safe_load(io.open(p, encoding="utf-8")) or []:
                others.append((e["id"], e["question"], tri(e["question"])))

    print("形狀驗證集 %d 題\n" % len(entries))
    rows_cache = {}
    for e in entries:
        qid = e["id"]
        try:
            df = db.execute_to_dataframe(e["sql"])
        except Exception as exc:
            bad[1].append((qid, "%s: %s" % (type(exc).__name__, exc)))
            continue
        if len(df) == 0:
            bad[1].append((qid, "GT 是空集合"))
            continue
        if len(df) > ROW_MAX:
            bad[5].append((qid, "%d 列 > %d" % (len(df), ROW_MAX)))
        rows_cache[qid] = len(df)
        gt_rows = to_rows(df)

        trap = e.get("trap_sql")
        if trap:
            try:
                tdf = db.execute_to_dataframe(trap)
            except Exception as exc:
                bad[2].append((qid, "%s: %s" % (type(exc).__name__, exc)))
                continue
            m = match_ordered if e.get("ordered") else match_unordered
            if m(to_rows(tdf), gt_rows):
                bad[3].append((qid, "天真寫法也被判對（GT %d 列 / trap %d 列）"
                               % (len(df), len(tdf))))
        elif e.get("family") != "control":
            bad[3].append((qid, "沒有 trap_sql，而 family 不是 control"))

        t = tri(e["question"])
        hi = max(((jaccard(t, o), oid) for oid, _, o in others), default=(0.0, None))
        if hi[0] > DUP_MAX:
            bad[4].append((qid, "與 #%s 相似 %.2f" % (hi[1], hi[0])))

        if not any(w in e["question"] for w in SETTLED):
            for table, cond in ALT_READING.items():
                alt = with_alt(e["sql"], table, cond)
                if not alt:
                    continue
                try:
                    adf = db.execute_to_dataframe(alt)
                except Exception:
                    continue
                m = match_ordered if e.get("ordered") else match_unordered
                if not m(to_rows(adf), gt_rows):
                    bad[6].append((qid, "%s.%s 換個讀法答案就變（GT %d 列 / 另一讀法 %d 列）"
                                   % (table, cond.split()[0], len(df), len(adf))))
                    break

    names = {
        1: "GT 可執行且非空",
        2: "trap_sql 可執行",
        3: "鑑別力（天真寫法必須被判錯）",
        4: "不與開發集／驗收集重複",
        5: "列數 <= %d" % ROW_MAX,
        6: "旗標歧義（換個讀法答案不能變）",
    }
    fail = 0
    for k in sorted(names):
        n = len(bad[k])
        fail += n
        print("[%d] %-28s %s" % (k, names[k], "OK" if not n else "✗ %d 題" % n))
        for qid, why in bad[k]:
            print("      #%s  %s" % (qid, why))

    fam = {}
    for e in entries:
        fam[e.get("family", "?")] = fam.get(e.get("family", "?"), 0) + 1
    print("\n分布 " + "　".join("%s %d" % kv for kv in sorted(fam.items())))
    if rows_cache:
        v = sorted(rows_cache.values())
        print("GT 列數　中位 %d　最大 %d" % (v[len(v) // 2], v[-1]))
    print("\n%s" % ("全綠" if not fail else "✗ 共 %d 項待修" % fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
