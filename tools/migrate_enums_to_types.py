# -*- coding: utf-8 -*-
"""把值域從 YAML 搬進欄位型別 —— YAML 只留語意，DDL 帶 enum。

為什麼
====================================================================
`utils/table_semantics.yaml` 裡有兩個不同的東西都叫 enum：`clauses` 的
`kind: enum`（一個散文句型）與每張表的 `enums:` 區塊（值域）。第二個
本來就不該住在那裡 —— **值域的家是欄位型別**。這個庫 1071 欄有 0 個
ENUM、0 個 CHECK，所以 `enums:` 一直在替資料庫沒做的事收拾殘局。

搬完之後：

    值域      COLUMN_TYPE 的 enum('白','粉','藍','銀','黑')
              → `tools/gen_ddl.py` 本來就讀 COLUMN_TYPE，自動帶進 DDL
    語意      YAML 的 clauses（content / usage / facet / bound / ptr）
    字面值    封閉值域走型別；開放值域（商品名、優惠碼）走值索引

還有一個附帶好處：`value_index._keep` 讀的是 COLUMN_COMMENT 與
TABLE_COMMENT，**不讀 COLUMN_TYPE**。值放進型別，DDL 帶得到，值索引
又不會把它踢掉 —— `enum-codes-block-the-value-index` 那個衝突消失。

哪些欄位算值域：這是宣告，不是掃出來的
====================================================================
試過兩個自動判準，兩個都不行：

    時間切分（後半段有沒有帶進新值）  38 欄樣本 <20 列，測不了
    值的形狀（是不是被公式產出來的）  把 J2..J8、B2/C1、IP67 全誤判

`J2..J8`（職等階梯，設計出來的）與 `DF-11..DF-79`（瑕疵代碼登記簿，會一直長）
在字串結構上一模一樣。**資料裡沒有這個差別，它是語意。**

所以 `NOT_A_DOMAIN` 是人判定一次、寫死在這裡的清單，不是演算法的輸出。
以後閘門只准報告，不准自動補宣告。

ORDER BY 的地雷
====================================================================
MySQL 的 ENUM **依宣告順序排序，不依字串定序**。四組題庫裡有 30 題
`ORDER BY` 落在待型別化的欄位上，直接 ALTER 下去那 30 題的答案會無聲改掉，
而驗收集的 GT 已經封存。

擋法：宣告順序就取資料庫自己的定序順序（`ORDER BY col`），於是
ordinal 順序 == 定序順序，`ORDER BY` 行為逐位元不變。死值（宣告了、
資料 0 筆）沒有資料位置，用 MySQL 自己排一次字面值清單來定位。

驗收：四組題庫的 **GT SQL 在遷移前後回傳完全相同的結果**。這個檢查
不讀任何模型輸出，所以不碰驗收集的協定。

用法
    python tools/migrate_enums_to_types.py --baseline   # 先存基準
    python tools/migrate_enums_to_types.py --plan       # 看 ALTER 長怎樣
    python tools/migrate_enums_to_types.py --apply      # 改，然後自動對帳
"""
import argparse
import hashlib
import io
import json
import os
import sys
from decimal import Decimal

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

PATH = os.path.join(_ROOT, "utils", "table_semantics.yaml")
BASELINE = os.path.join(_ROOT, "eval", "results", "_enum_migration_baseline.json")
BANKS = [
    ("開發集", os.path.join(_ROOT, "eval_ground_truth.yaml")),
    ("形狀驗證集", os.path.join(_ROOT, "eval", "validation_shapes.yaml")),
    ("風格驗證集", os.path.join(_ROOT, "eval", "validation_style.yaml")),
    ("驗收集", os.path.join(_ROOT, "eval", "testset_holdout.yaml")),
]

# 人判定一次：這些欄位**不是值域**，宣告要移掉，值改走值索引。
# 每一條都寫理由 —— 沒有理由的排除項下一個人就會把它加回來。
#
# 判準：**這組值是誰決定的。**
#   這門生意設計的  → 值域。加一個成員是刻意的產品決策，本來就該改 schema。
#                     訂單狀態、會員等級、退貨原因、顏色都屬於這一類。
#   外面的世界決定的 → 不是值域。ISO 國別／幣別／語言碼、閘道回傳的文字、
#                     UTM 參數、外部評級標準，或會機械性長大的登記簿
#                     （郵遞區號、瑕疵代碼、優惠碼、被稽核的表名、樓層）。
#                     這些欄位新來一列就可能要新值，而那時候不該被迫改 schema。
NOT_A_DOMAIN = {
    # 會機械性長大的登記簿
    "payment_profiles.coupon_code_applied": "優惠碼。每檔活動都會有新的，沒有封閉的值域",
    "store_profiles.postal_code":           "郵遞區號。是資料不是值域，開新店就有新值",
    "store_profiles.mon_open":              "營業時間字串。資料裡已經有 3 種宣告沒寫的",
    "store_profiles.floor_no":              "樓層。開在 4F 或 B2 就有新值",
    "return_profiles.defect_code":          "瑕疵代碼登記簿（DF-nn）。資料裡已經有 9 種宣告沒寫的，會一直長",
    "audit_log.table_name":                 "被異動的表名。加一張表就要多一個值，稽核表不該被 schema 變更綁住",
    # 外部標準的碼空間
    "product_specs.waterproof_rating":      "IP 防水等級。標準本身的碼空間很大（IPX0~8、IP0X~6X 及組合）",
    "order_profiles.ip_country":            "ISO 國別碼。下一張來自新加坡的單就存不進去",
    "payment_profiles.card_country":        "ISO 國別碼。發卡國不是我們能列舉的",
    "supplier_profiles.registered_country": "ISO 國別碼。多一個泰國供應商是日常，不是 schema 變更",
    "order_profiles.currency_code":         "ISO 幣別碼",
    "payment_profiles.currency_code":       "ISO 幣別碼",
    "campaign_profiles.currency_code":      "ISO 幣別碼。目前只有 TWD，但那是資料不是值域",
    "customer_profiles.preferred_language": "BCP 47 語言標籤",
    "support_ticket_profiles.intake_language": "BCP 47 語言標籤",
    "review_profiles.translated_from":      "BCP 47 語言標籤。來源語言可以是任何一種",
    "payment_profiles.avs_result":          "AVS 地址驗證碼。是發卡組織定義的一組碼，不是我們定的",
    # 外部系統寫進來的自由文字
    "payment_profiles.response_message":    "金流閘道的回應訊息。是對方的自由文字，今天只有『核准』純屬巧合",
    "order_profiles.utm_source":            "UTM 追蹤參數，行銷人員自己填",
    "order_profiles.utm_medium":            "UTM 追蹤參數，行銷人員自己填",
}


def declared(data):
    """{(表, 欄): [宣告的值]} —— values 與 dead_ok 都算，dead_ok 是還沒發生的合法值。"""
    out = {}
    for t, v in (data["tables"] or {}).items():
        for c, info in (v.get("enums") or {}).items():
            if "%s.%s" % (t, c) in NOT_A_DOMAIN:
                continue
            out[(t, c)] = [str(x) for x in
                           list(info.get("values") or {}) + list(info.get("dead_ok") or {})]
    return out


def _norm(v):
    if isinstance(v, Decimal):
        return float(round(v, 6))
    return str(v)


def bank_fingerprints(conn):
    """四組題庫每一條 GT SQL 的結果指紋。順序也算進去 —— ORDER BY 正是風險所在。"""
    fp = {}
    for name, path in BANKS:
        for e in yaml.safe_load(io.open(path, encoding="utf-8")):
            pairs = [("", e.get("sql"))]
            pairs += [("~alt%d" % i, s) for i, s in enumerate(e.get("alt_sql") or [])]
            for tag, sql in pairs:
                if not sql:
                    continue
                key = "%s#%s%s" % (name, e["id"], tag)
                try:
                    rows = [[_norm(x) for x in r] for r in conn.execute(text(sql))]
                    fp[key] = hashlib.sha256(
                        json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]
                except Exception as ex:
                    fp[key] = "ERR:" + type(ex).__name__
    return fp


def collation_order(conn, tb, co, vals):
    """照這一欄自己的定序把宣告值排序 —— 讓 ENUM 的 ordinal 順序 == 定序順序。"""
    coll = conn.execute(text(
        "SELECT COLLATION_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA=DATABASE() "
        "AND TABLE_NAME=:t AND COLUMN_NAME=:c"), {"t": tb, "c": co}).scalar()
    union = " UNION ALL ".join("SELECT :v%d AS v" % i for i in range(len(vals)))
    q = "SELECT v FROM (%s) x ORDER BY v COLLATE %s" % (union, coll)
    return [r[0] for r in conn.execute(text(q), {"v%d" % i: v for i, v in enumerate(vals)})]


def alters(conn, decl):
    """產出 ALTER 語句。型別以外的一切（NULL、預設值、註解）都原樣抄回去。"""
    out = []
    for (tb, co), vals in sorted(decl.items()):
        m = conn.execute(text(
            "SELECT IS_NULLABLE, COLUMN_DEFAULT, COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME=:t AND COLUMN_NAME=:c"),
            {"t": tb, "c": co}).one()
        ordered = collation_order(conn, tb, co, sorted(set(vals)))
        lst = ", ".join("'%s'" % v.replace("'", "''") for v in ordered)
        parts = ["`%s` ENUM(%s)" % (co, lst)]
        parts.append("NULL" if m[0] == "YES" else "NOT NULL")
        if m[1] is not None:
            parts.append("DEFAULT '%s'" % str(m[1]).replace("'", "''"))
        if m[2]:
            parts.append("COMMENT '%s'" % m[2].replace("'", "''"))
        out.append((tb, co, "ALTER TABLE `%s` MODIFY COLUMN %s" % (tb, " ".join(parts))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline", action="store_true")
    ap.add_argument("--plan", action="store_true")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    data = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    decl = declared(data)
    db = get_db_manager(MYSQL_URI)

    if args.baseline:
        with db.engine.connect() as conn:
            fp = bank_fingerprints(conn)
        io.open(BASELINE, "w", encoding="utf-8").write(
            json.dumps(fp, ensure_ascii=False, indent=1))
        err = [k for k, v in fp.items() if v.startswith("ERR:")]
        print("基準存好：%d 條 GT SQL 的結果指紋 → %s" % (len(fp), BASELINE))
        print("其中 %d 條本來就跑不起來（遷移前後都一樣，不算回歸）：" % len(err))
        for k in err[:10]:
            print("   %s %s" % (k, fp[k]))
        return 0

    with db.engine.connect() as conn:
        plan = alters(conn, decl)

    if args.plan:
        print("要型別化 %d 欄，分佈在 %d 張表。排除 %d 欄（不是值域）：\n"
              % (len(plan), len({t for t, _, _ in plan}), len(NOT_A_DOMAIN)))
        for k, why in sorted(NOT_A_DOMAIN.items()):
            print("   %-42s %s" % (k, why))
        print()
        for _, _, s in plan[:6]:
            print("   " + s)
        print("   ...（其餘 %d 條）" % (len(plan) - 6))
        return 0

    if not args.apply:
        print("要 --baseline / --plan / --apply 其中一個。")
        return 1

    if not os.path.exists(BASELINE):
        print("✗ 沒有基準檔。先跑 --baseline，否則遷移之後無從對帳。")
        return 1
    before = json.load(io.open(BASELINE, encoding="utf-8"))

    # 先驗完全部再動手。**MySQL 的 DDL 會隱式提交**，`engine.begin()` 包不住它 ——
    # 第 56 條炸掉的時候前 55 條已經生效了，資料庫會停在半遷移的狀態。
    # 唯一安全的順序是把所有會炸的原因在第一條 ALTER 之前就找出來。
    with db.engine.connect() as conn:
        short = []
        for (tb, co), vals in sorted(decl.items()):
            got = {str(r[0]) for r in conn.execute(text(
                "SELECT DISTINCT `%s` FROM `%s` WHERE `%s` IS NOT NULL" % (co, tb, co)))}
            extra = got - set(vals)
            if extra:
                short.append((tb, co, sorted(extra)))
    if short:
        print("✗ %d 欄的資料裡有宣告以外的值 —— ALTER 會截斷資料，一條都不動：" % len(short))
        for tb, co, ex in short:
            print("   %-42s 資料多出 %s" % (tb + "." + co, ex))
        print("\n先決定：把這些值補進宣告，還是這一欄根本不是值域（加進 NOT_A_DOMAIN）。")
        return 1

    with db.engine.begin() as conn:
        for tb, co, s in plan:
            conn.execute(text(s))
    print("已改 %d 欄。" % len(plan))

    with db.engine.connect() as conn:
        after = bank_fingerprints(conn)
    diff = [k for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k)]
    print("\n[驗收] 四組題庫 %d 條 GT SQL，遷移前後結果不同的：%d 條" % (len(after), len(diff)))
    for k in diff[:25]:
        print("   %-24s %s → %s" % (k, before.get(k), after.get(k)))
    if diff:
        print("\n✗ 有題目的答案被改掉了。ENUM 的排序或值域有問題，回滾再查。")
        return 1
    print("   四組題庫的 GT 答案逐條相同 —— ORDER BY 的地雷沒有踩到。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
