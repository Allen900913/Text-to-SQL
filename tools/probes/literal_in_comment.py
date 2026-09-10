# -*- coding: utf-8 -*-
"""偵測器：schema 註解裡出現了某個欄位的**資料值字面**。

為什麼要有這支
====================================================================
`value_index._keep()` 的第 ① 條會丟掉「出現在任何 schema 註解裡的值」。
所以註解每寫一次具體值，就等於把那個值從值索引刪掉一次。`_keep` 的
docstring 當初就寫下這個反作用（「表註解不該再舉具體實體名當例子」），
但沒有人在**寫註解的時候**檢查得到 —— 於是它只會在下游沉默地少命中。

實例：`order_cancellations` 的指路標寫「訂單是否已取消請看
orders.status = 'CANCELLED'」，一句話讓 `CANCELLED` 對 4 題失效
（`#10`/`#22`/`#23`/`#54`）。句子本身是對的、有用的，錯的只是引用了字面值。
**修偵測器不修個案**：所以這支掃全部註解，不是為那一句寫的。

命中清單不是待辦清單
====================================================================
一處引用只有在「那個值本來就報得出來」時才有代價，所以分三級：

    有代價    值住 <= VALUE_MAX_TABLES 張表 —— 拿掉引用就能解鎖，該修
    無代價    值跨表太多（`NONE` 住 6 張表），報出來也沒有鑑別力，本來就不會報
    刻意保留  撐著 `expect: empty` 題的前提（`payments.status` 的 FAILED 給
              `#104`、`customer_profiles.risk_flag` 給 `#159`）—— **不准修**

改法：指路要寫欄位的中文語意，不要寫值。值的家在 `enums` 宣告
（走 `enum_text()` 直送生成器）與值索引。
"""
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _p in (_ROOT, os.path.join(_ROOT, "tools")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.value_index import (  # noqa: E402
    _MIN_LEN, MYSQL_URI, VALUE_MAX_TABLES,
)
from strip_enum_codes import FORBIDDEN  # noqa: E402

# 要保護的是哪些值
# --------------------------------------------------------------------
# 不是「所有出現在註解裡的資料值」—— 那會抓到 265 處，而其中絕大多數
# （`業務`、`訂單`、`products`、`會員`）正是 `_keep` 第 ① 條**故意**要排除的
# 概念詞。把概念詞踢出值索引是那條規則的用意，不是損失（原型的精準度
# 15.4% 幾乎全毀在概念詞上）。所以偵測器要問的是更窄的問題：
#
#     這個值被排除掉，**是不是一個損失**？
#
# 會被問句逐字引用的值有兩種：enum 代碼，以及實體名（「iPhone 15」）。
# 實體名現在不會出現在註解裡，所以判準用兩個來源的聯集：
#
#     ① `enums` 宣告過的值 —— **權威來源**。宣告本身就是一句
#        「這是代碼不是散文」，比任何字形猜測可靠
#     ② 代碼字形的安全網 —— 接住還沒補宣告的欄位
#
# 再套 `_keep` 的第 ② 條（純 ASCII 且 < 4 字元不收）去掉假警報：
# 2 字元的 `TW`／`OK`／`C2`／`J8` 本來就進不了索引，報它們是雜訊。
#
# 比對方式照抄 `_keep`：`value.lower() not in schema_text`，是**子字串**
# 不是詞邊界 —— 用詞邊界會漏掉「評價語言：zh-TW／en／ja」這種值被別的
# 字元黏住的情形。
_CODE = re.compile(r"[A-Z][A-Z0-9_]{2,}")


def _indexable(v: str) -> bool:
    """`_keep` 的第 ② 條。第 ① 條正是這支要偵測的東西，不能拿來當條件。"""
    return len(v) >= _MIN_LEN and not (v.isascii() and len(v) < 4)


def _protected() -> set[str]:
    from langgraph_sql.utils.table_semantics import enums
    return {str(v) for e in enums().values() for v in (e.get("values") or {})}


def main() -> int:
    declared = _protected()
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        tcm = {t.lower(): (c or "") for t, c in conn.execute(text(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"))}
        ccm = {(t.lower(), c.lower()): (m or "") for t, c, m in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()"))}
        cols = conn.execute(text(
            "SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE IN ('varchar','char','enum')")).fetchall()
        owner: dict[str, set[str]] = {}
        for t, c in cols:
            for (v,) in conn.execute(text(
                    f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL")):
                v = str(v).strip()
                if _indexable(v) and (v in declared or _CODE.fullmatch(v)):
                    owner.setdefault(v, set()).add(t)

    tiers: dict[str, list] = {"有代價": [], "無代價": [], "刻意保留": [], "機制缺陷": []}
    scan = [(None, "表 " + t, m) for t, m in tcm.items()]
    scan += [(k, "欄 %s.%s" % k, m) for k, m in ccm.items()]
    for key, where, cm in scan:
        low = cm.lower()
        for v in sorted(v for v in owner if v.lower() in low):
            tabs = sorted(owner[v])
            # `_keep` 比的是**子字串**，所以「主要聯絡方式在 customers」這句
            # 會讓 `CUSTOMER` 這個代碼出局 —— 值卡在表名／欄位名裡面，
            # 不是被當成值引用（`.` 也算識別字的一部分 —— `products.category`
            # 裡的 `category` 是欄位名不是值）。**改註解修不掉**，
            # 這是 `_keep` 本身的過寬，該修的是機制不是句子。
            if not re.search(r"(?<![a-z0-9_.])%s(?![a-z0-9_.])" % re.escape(v.lower()), low):
                tiers["機制缺陷"].append((where, v, tabs, cm))
                continue
            tier = ("刻意保留" if key in FORBIDDEN else
                    "有代價" if len(tabs) <= VALUE_MAX_TABLES else "無代價")
            tiers[tier].append((where, v, tabs, cm))

    print("掃過 %d 張表註解 + %d 個欄位註解，資料裡的代碼值 %d 種　"
          "VALUE_MAX_TABLES=%d" % (len(tcm), len(ccm), len(owner), VALUE_MAX_TABLES))
    for tier in ("有代價", "無代價", "刻意保留", "機制缺陷"):
        rows = tiers[tier]
        print("\n[%s] %d 處" % (tier, len(rows)))
        for where, v, tabs, cm in rows:
            print("    %-42s%-14s住 %d 張表：%s"
                  % (where, v, len(tabs), ",".join(tabs)[:34]))
            if tier == "有代價":
                print("        " + cm[:92])
    if not tiers["有代價"]:
        print("\n[通過] 沒有『有代價』的引用。")
        return 0
    print("\n[不通過] 上面『有代價』那幾處要改：指路寫欄位的中文語意，不要寫值。")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
