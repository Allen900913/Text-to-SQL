# -*- coding: utf-8 -*-
"""閘門：低基數欄位的值，到得了模型嗎。

問題長什麼樣
====================================================================
    #3017「黑色款是哪些東西？什麼尺寸？」
         模型 WHERE color = '黑色'   → 0 列
         資料  color 只有 '藍','黑','銀','白','粉'

模型不是猜錯，是**沒東西可看**。`product_variants` 沒有 `enums` 區塊，
欄位註解只寫「顏色」，而值索引因為 `_MIN_LEN = 2` 收不進單字的值 ——
五個顏色一個都沒送出去，三條通道全斷。

三條通道
====================================================================
一個字面值要能被寫進 WHERE，得從某條路到達模型：

    ① 欄位型別        ENUM('白','粉',...) 走 DDL       ← 值域的家
    ② 欄位註解        走 DDL
    ③ 值索引          問句點名時附證據（value_index）

② 的射程有限（註解是寫語意的，不是列值的；而且註解裡寫了值，`_keep`
就會把那個值踢出值索引 —— 見 `enum-codes-block-the-value-index`）。
③ 有自己的門檻：長度 <2 不收、純 ASCII 長度 <4 不收、出現在任何註解裡
不收。所以封閉值域真正的家是 ①（`enum-declares-domain-not-snapshot`）——
2026-09-12 起那是 `COLUMN_TYPE`，不是 YAML。

判準（**與題目無關**，這是重點）
====================================================================
低基數字串欄（distinct <= MAX_CARD）的值，三條通道**一條都不通**
→ 紅燈。

刻意不看「哪些題失敗了」來決定補哪些欄位 —— 那是照著答案改。
判準只讀 schema 與資料，所以它對還沒出過的題一樣成立。

不在射程內的東西（說清楚免得綠燈被過度解讀）
====================================================================
· 只掃字串欄。數值與日期的「值」不是字面值問題。
· 只掃低基數欄。高基數欄（商品名、客戶名）本來就該走值索引，
  不該塞進 enum_text。
· 通道②只認「值出現在**該欄自己的**註解裡」。
· 綠燈代表「有一條路通」，不代表模型一定會走那條路。

用法
    python tools/check_value_reachability.py
"""
import io
import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils import value_index as vi  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

PATH = os.path.join(_ROOT, "utils", "table_semantics.yaml")

# 低基數的上限。超過這個數就不是「值域」而是「資料」，該走值索引。
# 8 是照現有 enums 宣告的分佈挑的（最大的宣告是 8 個值），不是調出來的。
MAX_CARD = 8


def _schema_text(conn) -> str:
    """`value_index._keep` 讀的那一份 —— 資料庫的表註解＋欄位註解。"""
    return " ".join(
        [r[0] or "" for r in conn.execute(text(
            "SELECT TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"))]
        + [r[0] or "" for r in conn.execute(text(
            "SELECT COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()"))]).lower()


def scan(conn, data) -> list:
    """回傳 [(表, 欄, 欄位註解, 值清單)] —— 三條通道全不通的欄位。"""
    st = _schema_text(conn)
    # 通道① 的權威從 2026-09-12 起是 **COLUMN_TYPE**，不是 YAML。
    # 值域搬進型別之後，`enums:` 只剩下刻意不當值域的那些欄位。
    typed = {(r[0], r[1]) for r in conn.execute(text(
        "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE IN ('enum','set')"))}
    out = []
    for tb, co, cm in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND DATA_TYPE IN ('varchar','char') ORDER BY TABLE_NAME, ORDINAL_POSITION")):
        spec = data["tables"].get(tb)
        if not spec or co in (spec.get("enums") or {}) or (tb, co) in typed:
            continue                                    # ① 通
        try:
            vals = [str(r[0]) for r in conn.execute(text(
                "SELECT DISTINCT `%s` FROM `%s` WHERE `%s` IS NOT NULL" % (co, tb, co)))]
        except Exception:
            continue
        if not 1 <= len(vals) <= MAX_CARD:
            continue
        cm = cm or ""
        if any(v in cm for v in vals):
            continue                                    # ② 通
        if any(vi._keep(v, st) for v in vals):
            continue                                    # ③ 通
        out.append((tb, co, cm, sorted(vals)))
    return out


def main() -> int:
    # 沒有 --fix：封閉與否是人的判斷，不是掃描的輸出（見檔頭）。
    db = get_db_manager(MYSQL_URI)
    data = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    with db.engine.connect() as conn:
        blocked = scan(conn, data)

    print("閘門：低基數欄位的值到得了模型嗎（distinct <= %d 的字串欄）\n" % MAX_CARD)
    if not blocked:
        print("綠燈 —— 每個低基數欄至少有一條通道。")
        print("（綠燈代表有路可走，不代表模型一定會走。高基數欄不在射程內。）")
        return 0

    print("✗ %d 個欄位三條通道全不通 —— 模型寫不出這些值：" % len(blocked))
    for tb, co, cm, vals in blocked:
        print("   %-44s 註解「%s」" % (tb + "." + co, cm[:24]))
        print("   %-44s 值 %s" % ("", vals))
    print("\n改法：人先回答一個問題 ——「這一欄是封閉值域嗎」。")
    print("      是   → ALTER TABLE ... MODIFY COLUMN x ENUM(...)，然後跑 gen_ddl.py --write")
    print("      不是 → 開放集合（登記簿、外部標準碼），值走值索引，把它寫進")
    print("             tools/migrate_enums_to_types.py 的 NOT_A_DOMAIN 並附理由")
    print("\n這支**刻意不提供 --fix**。2026-09-12 的教訓：自動照 distinct <= 8 補宣告，")
    print("把 postal_code、defect_code、coupon_code、audit_log.table_name 全當成了值域。")
    print("封閉與否是語意，不在資料裡 —— J2..J8（職等階梯）與 DF-11..DF-79（會長大的")
    print("登記簿）在字串結構上一模一樣。掃得出候選，判不出答案。")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
