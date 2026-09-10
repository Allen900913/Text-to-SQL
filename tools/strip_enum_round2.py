# -*- coding: utf-8 -*-
"""E17+E18：把最後 11 個欄位的值域搬出註解 —— 「註解寫語意，值寫在 enums」。

為什麼要有第二輪
================================================================
E15（`strip_column_enums.py`）用的是自動判準（`should_strip`：>=2 個代碼
token 且至少一個是活值），掃過 78 欄。剩下的 11 欄每一欄都是**自動規則
接不住的個案**，所以這裡用明表，不用正則 —— 每一條都要看得見理由：

  ① 只有一個代碼        `hazard_class`「NONE 表示非危險品」—— 門檻要 2 個
  ② 代碼是別欄的值      `discount_value` 是 DECIMAL，PERCENT/FIXED 是
                        `discount_type` 的值。這是**跨欄位解釋**不是值域，
                        改寫成指路標，不是刪掉
  ③ 我上一輪列進 FORBIDDEN 的兩欄 —— 那是判斷錯誤，見下

FORBIDDEN 撤銷的理由（2026-09-10）
================================================================
E15 把 `payments.status` 與 `customer_profiles.risk_flag` 排除，理由是
`#104`/`#159` 是 `expect: empty` 題，考「註解列了某個值 ≠ 資料裡真的有」，
清掉註解等於拆掉考點。**查了宣告之後發現這個理由站不住**：

  `payments.status` 的 arm 是「送」不是 `dup` —— `enum_text()` **現在就已經**
  把 SUCCESS/FAILED/REFUNDED 直送生成器。註解裡那份是純重複，清掉
  `#104` 的陷阱一根寒毛都不會少。

  `risk_flag` 不一樣，有真的坑：宣告的值是 NONE/WATCH，**沒有 HIGH**
  （閘門 [4b] 拿資料驗值域，資料裡沒有 HIGH）。直接清會讓 HIGH 從模型
  視野裡整個消失，`#159` 就真的死了。所以要先把 HIGH 補進宣告當**死代碼**
  —— [4b] 本來就支援死代碼（現在計 12 個），這正是它存在的用途。

陷阱沒有被拆掉，是從 DDL 註解層搬到值域區塊層。但「模型讀值域區塊」與
「模型讀 DDL 註解」是不是同一回事，是**行為問題不是位元問題**，所以判準
⑥ 要用 n=8 實跑（memory: `n3-per-question-is-not-evidence`）。

已知負債：`product_profiles.hazard_class` 的宣告是 `arm: gap`（宣告但不送）。
清掉註解之後「NONE 表示非危險品」就沒有任何一層在送了，要等 TS_ENUM_GAPS
那個臂帶著自己的判準翻正才會回來。GT `#259` 只 SELECT 這一欄、不用字面值
過濾，所以量不到 —— 這是**暴露不是缺陷**，但要記著
（memory: `gates-measure-exposure-not-defects`）。

用法：
    python tools/strip_enum_round2.py            # 預覽，不寫
    python tools/strip_enum_round2.py --write    # 寫 YAML（還要跑 sync 才進 DB）
    python tools/strip_enum_round2.py --revert   # 從 git 還原 YAML
"""
import io
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.join(_ROOT, "tools")):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.value_index import MYSQL_URI  # noqa: E402

PATH = os.path.join(_ROOT, "utils", "table_semantics.yaml")

# (表, 欄位) -> 新註解。值一律搬去 enums，註解只留語意。
REWRITE = {
    # 撤銷 FORBIDDEN
    ("payments", "status"):                     "付款狀態，與訂單狀態不同意義",
    ("customer_profiles", "risk_flag"):         "風控標記",
    # 原本判為「無代價」（NONE 住 8 張表，值索引本來就擋掉）—— 清掉是為了一致
    ("campaign_profiles", "ab_test_group"):     "A/B 測試分組",
    ("product_profiles", "hazard_class"):       "危險品分類",
    ("promotion_profiles", "member_tier_required"): "需要的會員等級",
    ("return_profiles", "defect_code"):         "瑕疵代碼",
    # 原本判為「有代價」—— 這些值真的被 _keep 擋著
    ("campaign_profiles", "target_gender"):     "鎖定性別",
    ("customer_profiles", "gender"):            "性別",
    ("store_profiles", "mon_open"):             "週一營業時間",
    # 跨欄位解釋，改指路標不刪語意（同 order_cancellations 那次的修法）
    ("promotions", "discount_value"):           "折扣數值，是百分比還是折抵金額由 discount_type 決定",
}

# 補宣告：這幾欄註解裡有值、`enums` 裡卻沒有。不補就是清掉之後直接失血。
#
# 這三欄都不是**封閉列舉**，是「開放集合 ＋ 一個哨兵值」——
#     member_tier_required   哨兵 NONE，其餘是會員等級名稱（銀卡／金卡…）
#     defect_code            哨兵 NONE，其餘是 DF-NN 流水代碼（現有 9 種）
#     mon_open               哨兵 CLOSED，其餘是 "10:00-20:00" 這種時段字串
# 所以宣告**只列哨兵**是對的，不是漏列。但這樣一份宣告看起來會像完整值域，
# 會誤導生成器寫出 `IN ('NONE')` 這種東西 —— 所以 `description` 一定要
# 自己講清楚它是部分列舉。這是 SENTINEL 這一組存在的全部理由。
ADD_ENUMS = {
    ("promotion_profiles", "member_tier_required"):
        {"description": "需要的會員等級；以下只列哨兵值，其餘是會員等級名稱",
         "values": {"NONE": "不限會員等級"}},
    ("return_profiles", "defect_code"):
        {"description": "瑕疵代碼；以下只列哨兵值，其餘是 DF-NN 格式的瑕疵代碼",
         "values": {"NONE": "非品質問題，沒有瑕疵代碼"}},
    ("store_profiles", "mon_open"):
        {"description": "週一營業時間；以下只列哨兵值，其餘是「10:00-20:00」這種時段字串",
         "values": {"CLOSED": "當日公休"}},
}

# 哨兵欄位：值域**本來就不封閉**，宣告只列哨兵。所以「宣告漏了活值」那條
# 完整性斷言對它們不成立，要豁免 —— 豁免寫在這裡，不寫在斷言裡，
# 這樣新增一個豁免會出現在 diff 上（memory: `silent-pass-is-not-a-pass`）。
SENTINEL = set(ADD_ENUMS)

# 死代碼：宣告得完整，但資料裡沒有。閘門 [4b] 會把它算進死代碼，那是對的 ——
# `#159` 考的正是「宣告有、資料沒有」。
# 說明只寫**代碼字面的直譯**，不寫流程推測。第一版我把 HIGH 寫成
# 「高風險，需人工複核」——「需人工複核」在任何來源裡都沒有，是我編的。
# 值域宣告會直送生成器，編出來的流程細節會被模型當事實用。
ADD_DEAD = {
    ("customer_profiles", "risk_flag"):     {"HIGH": "高風險"},
    # `B` 同理：欄位的值域本來就是 A/B/NONE，資料剛好只生出 A 與 NONE。
    # 不宣告的話 description 那句「A／B／NONE」就砍不掉（trim 要求每個
    # token 都是已宣告的值），代碼會卡在 description 裡出不來。
    ("campaign_profiles", "ab_test_group"): {"B": "B 組"},
}

# 這一輪**不碰**的欄位：discount_value 的值域住在 discount_type，不需要自己的宣告。
NO_ENUM_NEEDED = {("promotions", "discount_value")}


def main(write: bool) -> int:
    d = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        V = {}
        for t, c in REWRITE:
            try:
                V[(t, c)] = {str(v).strip() for (v,) in conn.execute(
                    text(f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL"))}
            except Exception:
                V[(t, c)] = set()

    problems, changed = [], []
    for (t, c), new in REWRITE.items():
        spec = d["tables"].get(t)
        if spec is None or c not in (spec.get("columns") or {}):
            problems.append(f"{t}.{c} 不在 YAML 裡")
            continue
        old = spec["columns"][c]
        spec["columns"][c] = new
        spec.setdefault("enums", {})
        if (t, c) in ADD_ENUMS:
            if c in spec["enums"]:
                problems.append(f"{t}.{c} 已有宣告，ADD_ENUMS 會覆蓋 —— 明表過期了")
            else:
                spec["enums"][c] = dict(ADD_ENUMS[(t, c)])
        if (t, c) in ADD_DEAD:
            e = spec["enums"].get(c)
            if e is None:
                problems.append(f"{t}.{c} 要補死代碼但沒有宣告")
            else:
                e["values"] = {**(e.get("values") or {}), **ADD_DEAD[(t, c)]}
        e = spec["enums"].get(c)
        if e is None and (t, c) not in NO_ENUM_NEEDED:
            problems.append(f"{t}.{c} 沒有 enums 宣告 —— 清了註解就失血")
        elif e is not None:
            # gap 保持 gap（那是另一個臂）；dup 一定要拿掉，註解裡已經沒有了
            if e.get("arm") == "dup":
                e.pop("arm")
            miss = V[(t, c)] - set(e.get("values") or {})
            if miss and e.get("arm") != "gap" and (t, c) not in SENTINEL:
                problems.append(f"{t}.{c} 宣告漏了活值 {sorted(miss)}")
            if (t, c) in SENTINEL and "只列哨兵" not in (e.get("description") or ""):
                problems.append(f"{t}.{c} 是哨兵欄位，description 要講明它是部分列舉")
        changed.append((t, c, old, new,
                        "值域在 discount_type" if (t, c) in NO_ENUM_NEEDED
                        else (e or {}).get("arm", "送")))

    for t, c, old, new, arm in changed:
        print(f"  {t}.{c}")
        print(f"      舊 {old}")
        print(f"      新 {new}   [enums arm={arm}]")
    print(f"\n改寫 {len(changed)} 欄；新增宣告 {len(ADD_ENUMS)}；補死代碼 {len(ADD_DEAD)}")
    if problems:
        print("\n✗ " + "\n✗ ".join(problems))
        print("未寫入。")
        return 1
    if not write:
        print("\n預覽而已，加 --write 才會寫 YAML。")
        return 0

    io.open(PATH, "w", encoding="utf-8").write(yaml.safe_dump(
        d, allow_unicode=True, sort_keys=False, default_flow_style=False, width=4096))

    # 判準 ④：被改寫的欄位，每個活值都要在 enum_text() 裡（gap 臂除外）
    et = subprocess.run(
        [sys.executable, "-c",
         "import sys;sys.path.insert(0,r'%s');from loguru import logger as l;l.remove();"
         "from langgraph_sql.utils.table_semantics import enum_text;"
         "sys.stdout.reconfigure(encoding='utf-8');print(enum_text(),end='')" % _ROOT],
        capture_output=True, text=True, encoding="utf-8").stdout
    bad = []
    for t, c, _, _, arm in changed:
        if arm == "gap" or (t, c) in NO_ENUM_NEEDED:
            continue
        # 哨兵欄位只驗哨兵值送得到，開放集合的其餘值本來就不該進 Prompt
        want = (set(ADD_ENUMS[(t, c)]["values"]) if (t, c) in SENTINEL
                else V[(t, c)])
        for v in sorted(want):
            if f"'{v}'" not in et:
                bad.append(f"{t}.{c} = {v}")
    print(f"\n[判準 ④] enum_text() {len(et)} 字元；活值缺漏 {len(bad)} 個"
          + ("" if not bad else "：" + str(bad)))
    print("YAML 已寫入。跑 tools/sync_table_comments.py --apply 才會進資料庫。")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    if "--revert" in sys.argv:
        subprocess.run(["git", "checkout", "--", "utils/table_semantics.yaml"], cwd=_ROOT)
        print("YAML 已從 git 還原")
        sys.exit(0)
    sys.exit(main("--write" in sys.argv))
