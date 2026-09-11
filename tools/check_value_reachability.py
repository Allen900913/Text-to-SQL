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

    ① enums 宣告      走 enum_text() 直送生成器      ← 值的家
    ② 欄位註解        走 DDL
    ③ 值索引          問句點名時附證據（value_index）

② 的射程有限（註解是寫語意的，不是列值的；而且註解裡寫了值，`_keep`
就會把那個值踢出值索引 —— 見 `enum-codes-block-the-value-index`）。
③ 有自己的門檻：長度 <2 不收、純 ASCII 長度 <4 不收、出現在任何註解裡
不收。所以真正的家是 ①（`comments-hold-meaning-values-live-in-enums`）。

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
    python tools/check_value_reachability.py --fix    # 補上 enums 宣告
"""
import io
import os
import subprocess
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
from langgraph_sql.utils.table_semantics import enum_text  # noqa: E402

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
    out = []
    for tb, co, cm in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT "
            "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE() "
            "AND DATA_TYPE IN ('varchar','char') ORDER BY TABLE_NAME, ORDINAL_POSITION")):
        spec = data["tables"].get(tb)
        if not spec or co in (spec.get("enums") or {}):
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
    fix = "--fix" in sys.argv
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
    if not fix:
        print("\n改法：在 utils/table_semantics.yaml 補 enums 宣告（值的家在這裡）。")
        print("      跑 `--fix` 自動補上，會印出 enum_text() 的完整差異。")
        return 1

    before = enum_text()
    for tb, co, cm, vals in blocked:
        data["tables"][tb].setdefault("enums", {})[co] = {
            # 描述用欄位註解的第一段 —— 與 add_missing_enums.py 同一套，
            # 不自己造詞。值不加中文註釋：造詞會把概念詞塞進值域，而
            # 概念詞是 dense 那一層的工作。
            "description": cm.split("：")[0].split("(")[0].strip() or co,
            "values": {v: "" for v in vals},
        }
    io.open(PATH, "w", encoding="utf-8").write(yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096))

    after = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,r'%s');"
         "from loguru import logger as l;l.remove();"
         "from langgraph_sql.utils.table_semantics import enum_text;"
         "sys.stdout.reconfigure(encoding='utf-8');print(enum_text(),end='')" % _ROOT],
        capture_output=True, text=True, encoding="utf-8").stdout
    b, a = before.split("\n"), after.split("\n")
    print("\n[驗收] enum_text() %d → %d 字元（全庫；scoped 呼叫只送有被選到的表）"
          % (len(before), len(after)))
    print("       多出 %d 行、少掉 %d 行" % (len(set(a) - set(b)), len(set(b) - set(a))))
    for ln in b:
        if ln not in a:
            print("       −%s" % ln)
    gone = [ln for ln in b if ln not in a]
    print("       %s" % ("沒有任何既有的行被動到。" if not gone
                         else "✗ 有既有的行不見了，上面那幾行要看清楚。"))
    return 1 if gone else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
