# -*- coding: utf-8 -*-
"""清掉「宣告了、資料裡 0 筆」的 enum 值，承重的那些改成登記。

問題長什麼樣
====================================================================
    payments.status  宣告 ['SUCCESS', 'FAILED', 'REFUNDED']
    資料裡只有        ['SUCCESS']

`enum_text()` 會把這三個值原封不動送給生成器。**系統親口告訴模型那兩個值
存在。** 模型照著寫 `status = 'REFUNDED'`，拿到 0 列，而且那個 0 看起來
完全合法 —— 沒有語法錯、沒有空表、沒有任何訊號說「你找的東西不在這裡」。

死值的代價不是「多送幾個字」，是**把失敗變靜默**。

為什麼閘門 [4b] 沒擋下來
====================================================================
它其實算出來了。`check_table_semantics.py` 的 [4b] 從一開始就在數 `dead`，
而且印在摘要行裡（「死代碼 14」）。但是：

    · 紅綠燈只看 `unlisted`（資料有、宣告沒列），`dead` 不影響判定
    · `dead` 的明細只在 `--show-all` 才印

所以 14 個死值以一個沒有人會去讀的數字的形式，公開地存在了很久。
[[silent-pass-is-not-a-pass]] 的另一種形狀：不是吞掉例外，是**算出來了
但不擋人**。這一支連同 [4b] 的修改一起，把它變成會擋人的東西。

不能一律刪 —— 有兩個是承重的
====================================================================
死值有時候是**題庫刻意撐著的**（[[schema-data-gaps-may-be-the-benchmark]]）：

    payments.status = 'FAILED'        撐開發集 #104（expect: empty）
    customer_profiles.risk_flag='HIGH' 撐開發集 #159（expect: empty）

那兩題問「有沒有失敗的付款／高風險客戶」，正確答案就是空集合。模型要寫得
出那個 WHERE 條件，才可能拿到那個空集合 —— 值不宣告，題目就不可能答對。

所以判準不是「刪掉死值」，是**死值必須登記理由**：

    values:
      SUCCESS: 成功
      FAILED: 失敗
    dead_ok:
      FAILED: 撐開發集 #104 的空集合題 —— 資料 0 筆是刻意的

`dead_ok` 是 `enum_text()` 讀不到的鍵，所以加它是位元中性的；它唯一的
讀者是閘門 [4b]。豁免寫在宣告裡、不寫成閘門內部的表名清單 —— 跟 [4b]
既有的「哨兵」豁免同一個作法，理由也一樣：寫在閘門裡的清單會跟資料一起
過期，而且不會有人發現。

射程
====================================================================
只處理**這一次掃出來的 14 個**。以後新長出來的死值由 [4b] 擋，不由這支清。
這支是一次性的遷移，跟 `add_missing_enums.py` 同一個位階。

用法
    python tools/strip_dead_enum_values.py --dry-run
    python tools/strip_dead_enum_values.py
"""
import io
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402

from langgraph_sql.utils.table_semantics import enum_text  # noqa: E402

PATH = os.path.join(_ROOT, "utils", "table_semantics.yaml")

# 承重：這個值撐著某一題的空集合，刪掉那題就不可能答對。
# 逐題查過四組題庫的 GT SQL（值字面 ＋ 欄位名同時出現才算數）。
KEEP = {
    "payments.status": {
        "FAILED": "撐開發集 #104（expect: empty）—— 資料 0 筆是題目要的答案",
    },
    "customer_profiles.risk_flag": {
        "HIGH": "撐開發集 #159（expect: empty）—— 資料 0 筆是題目要的答案",
    },
}

# 可清：四組題庫的 GT 一題都沒用到。
# （`dispute_status='OPEN'`／`audit_result='FAIL'` 在鬆比對下曾經命中 #165
#  #306 #3012，逐題看過都是**同名值撞到別張表**的假陽性 —— 那些 SQL 裡
#  根本沒有這個欄位。）
FREE = {
    "campaign_profiles.ab_test_group": ["B"],
    "employee_profiles.employment_type": ["INTERN"],
    "payment_profiles.dispute_status": ["OPEN"],
    "payments.status": ["REFUNDED"],
    "return_profiles.inspection_result": ["PARTIAL"],
    "return_profiles.package_condition": ["MISSING"],
    "return_profiles.compensation_type": ["GIFT_CARD"],
    "shipments.status": ["PREPARING", "RETURNED"],
    "store_profiles.audit_result": ["FAIL"],
    "supplier_profiles.audit_result": ["FAIL"],
    "support_ticket_profiles.satisfaction_label": ["SATISFIED"],
}


def _enum_text_in_subprocess() -> str:
    """重讀 YAML 之後的 enum_text() —— 模組在行程裡有快取，只能開子行程。"""
    return subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,r'%s');"
         "from loguru import logger as l;l.remove();"
         "from langgraph_sql.utils.table_semantics import enum_text;"
         "sys.stdout.reconfigure(encoding='utf-8');print(enum_text(),end='')" % _ROOT],
        capture_output=True, text=True, encoding="utf-8").stdout


def main() -> int:
    dry = "--dry-run" in sys.argv
    before = enum_text()
    d = yaml.safe_load(io.open(PATH, encoding="utf-8"))

    removed, kept, missing = [], [], []

    for field, vals in FREE.items():
        t, _, c = field.partition(".")
        info = ((d["tables"].get(t) or {}).get("enums") or {}).get(c)
        if info is None:
            missing.append(field)
            continue
        for v in vals:
            if v in (info.get("values") or {}):
                del info["values"][v]
                removed.append((field, v))
            else:
                missing.append("%s='%s'" % (field, v))

    for field, reasons in KEEP.items():
        t, _, c = field.partition(".")
        info = ((d["tables"].get(t) or {}).get("enums") or {}).get(c)
        if info is None:
            missing.append(field)
            continue
        for v, why in reasons.items():
            if v not in (info.get("values") or {}):
                missing.append("%s='%s'（要登記的值不見了）" % (field, v))
                continue
            info.setdefault("dead_ok", {})[v] = why
            kept.append((field, v, why))

    print("清掉的死值 %d 個：" % len(removed))
    for f, v in removed:
        print("   %-45s '%s'" % (f, v))
    print("\n登記為承重的 %d 個（值留著，enum_text 照送）：" % len(kept))
    for f, v, why in kept:
        print("   %-45s '%s'  %s" % (f, v, why))
    if missing:
        print("\n✗ 對不上的 %d 項 —— 這份清單過期了，先查清楚再跑：" % len(missing))
        for m in missing:
            print("   %s" % m)
        return 1

    if dry:
        print("\n--dry-run，沒有寫檔。")
        return 0

    io.open(PATH, "w", encoding="utf-8").write(yaml.safe_dump(
        d, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096))

    after = _enum_text_in_subprocess()
    gone = [ln for ln in before.split("\n") if ln not in after.split("\n")]
    added = [ln for ln in after.split("\n") if ln not in before.split("\n")]

    print("\n[驗收] enum_text() %d → %d 字元" % (len(before), len(after)))
    print("       少掉 %d 行、多出 %d 行" % (len(gone), len(added)))
    for ln in gone:
        print("       −%s" % ln)
    for ln in added:
        print("       +%s" % ln)

    # 唯一的合法差異就是被清掉的那些值。多一行少一行都要當場看見。
    want = {"    - '%s'" % v for _, v in removed}
    unexpected = [ln for ln in gone if ln.strip().split(" =")[0].strip() not in
                  {"- '%s'" % v for _, v in removed}]
    ok = not added and not unexpected
    print("       %s" % ("差異就是那 %d 個死值，沒有別的。" % len(removed) if ok
                         else "✗ 出現預期外的差異，上面那幾行要看清楚。"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
