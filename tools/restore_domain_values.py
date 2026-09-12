# -*- coding: utf-8 -*-
"""把 c7bc770 刪掉的 12 個值補回值域 —— 那次清理的判準是錯的。

那次刪掉的理由是「宣告了但資料 0 筆」。**值域跟資料筆數無關**：
`store_profiles.audit_result` 少了 `FAIL`，不代表稽核不會失敗，只代表
今天的資料裡還沒有一次失敗。型別化之後這件事會從「文字上的缺漏」變成
「資料庫物理上存不進去」—— ALTER 成 ENUM('CONDITIONAL','PASS') 之後，
第一間稽核不合格的門市就寫不進去了。

`dead_ok` 這個機制一起退場：它是為了讓閘門 [4b] 放行而發明的，而 [4b]
本身的極性就是反的。值域裡沒有「死值」這種東西，只有還沒發生的值。

用法：python tools/restore_domain_values.py
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml  # noqa: E402

PATH = os.path.join(_ROOT, "utils", "table_semantics.yaml")

# c7bc770 刪掉的，連同它們的語意。描述是照欄位本來的命名慣例寫的，
# 不是從題目抄的。
RESTORE = {
    "campaign_profiles.ab_test_group":            {"B": "B 組"},
    "employee_profiles.employment_type":          {"INTERN": "實習"},
    "payment_profiles.dispute_status":            {"OPEN": "爭議處理中"},
    "payments.status":                            {"REFUNDED": "已退款"},
    "return_profiles.inspection_result":          {"PARTIAL": "部分通過"},
    "return_profiles.package_condition":          {"MISSING": "包裝缺件"},
    "return_profiles.compensation_type":          {"GIFT_CARD": "禮物卡"},
    "shipments.status":                           {"PREPARING": "備貨中",
                                                   "RETURNED": "已退回"},
    "store_profiles.audit_result":                {"FAIL": "稽核不合格"},
    "supplier_profiles.audit_result":             {"FAIL": "稽核不合格"},
    "support_ticket_profiles.satisfaction_label": {"SATISFIED": "滿意"},
}


def main() -> int:
    data = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    added, promoted, missing = [], [], []
    for key, vals in RESTORE.items():
        tb, _, co = key.partition(".")
        info = ((data["tables"].get(tb) or {}).get("enums") or {}).get(co)
        if info is None:
            missing.append(key)
            continue
        for v, desc in vals.items():
            if v in (info.get("values") or {}):
                continue
            info.setdefault("values", {})[v] = desc
            added.append("%s '%s' —— %s" % (key, v, desc))

    # dead_ok 一起收掉：值域裡沒有死值，只有還沒發生的值。
    for tb, spec in data["tables"].items():
        for co, info in (spec.get("enums") or {}).items():
            for v, why in (info.pop("dead_ok", None) or {}).items():
                info.setdefault("values", {}).setdefault(v, "")
                promoted.append("%s.%s '%s'（原本登記為 %s）" % (tb, co, v, why[:24]))

    if missing:
        print("✗ 這些欄位在 YAML 裡找不到，先看清楚：", missing)
        return 1

    io.open(PATH, "w", encoding="utf-8").write(yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096))
    print("補回 %d 個值：" % len(added))
    for a in added:
        print("   +%s" % a)
    print("\ndead_ok 收掉 %d 個，併回一般的值：" % len(promoted))
    for p in promoted:
        print("   ~%s" % p)
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
