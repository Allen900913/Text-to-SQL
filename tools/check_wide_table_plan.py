"""寬表擴充計畫稽核 —— 建表**之前**跑，不碰資料庫。

為什麼要有這支（ARCHITECTURE.md §7.4 的「先宣告，再稽核，最後才灌」）：

    加表會靜默作廢防禦題（§6.2）。等建完表才跑 check_defence_gt.py，
    發現撞到探針就得 DROP 重來 —— 而 DROP 一張已經灌了資料的表，
    在這個專案裡是最容易手滑毀掉既有 GT 的操作。

    所以判準往前搬：**欄位還只是 YAML 裡的字串時就先檢查。**

檢查七項：
  [1] 活探針      欄位名／註解不得命中「沒有 acknowledged」的防禦題探針 → 命中即紅
  [2] 已裁決探針  命中不擋，但要列出來讓人確認沒有讓防禦題實質可答
  [3] 表名衝突    新表名不得與現有 80 張表重複
  [4] 掛載點存在  attach.to 必須是現有的表
  [5] 寬度達標    每張表要真的夠寬，否則這次擴充達不到目的
  [6] 題目覆蓋    每張新表都要有題（閘門第 6 項會擋，先在這裡擋）
  [7] 角色配額    multi_col ≥ 15、two_wide ≥ 3、discrim ≥ 4

用法：
    python tools/check_wide_table_plan.py
"""
import argparse
import io
import os
import re
import sys

import yaml
from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN_PATH = os.path.join(_ROOT, "tools", "wide_table_plan.yaml")

# 這次擴充要達到的目標（寫死在這裡，因為它們是「為什麼要做」本身）
MIN_COLS = 40
QUOTA = {"multi_col": 15, "two_wide": 3, "discrim": 4}


def existing_tables() -> set[str]:
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        return {r[0].lower() for r in conn.execute(text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"))}


def declared_columns(plan) -> list[tuple[str, str, str]]:
    """(表, 欄位, 註解) —— 與 check_defence_gt.load_columns() 同一個形狀，
    好讓兩支共用同一套探針比對邏輯。"""
    out = []
    for tname, spec in plan["tables"].items():
        for col in spec["columns"]:
            out.append((tname, col["name"], col.get("comment", "")))
        # 表註解也要掃 —— 它同樣會進 INFORMATION_SCHEMA、同樣被探針掃到
        out.append((tname, "<TABLE_COMMENT>", spec.get("table_comment", "")))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default=PLAN_PATH,
                    help="宣告檔路徑（第二批用 wide_table_plan_2.yaml）")
    args = ap.parse_args()
    log.remove()
    print(f"宣告檔：{os.path.relpath(args.plan, _ROOT)}")
    plan = yaml.safe_load(io.open(args.plan, encoding="utf-8"))
    cols = declared_columns(plan)
    tables = plan["tables"]
    # 探針政策**永遠**讀第一批那份，不讀 --plan 指的檔案 ——
    # 每批各抄一份就會漂移，而漂移的方向一定是「這批剛好沒抄到那個字」。
    policy = yaml.safe_load(io.open(PLAN_PATH, encoding="utf-8"))["probe_policy"]
    fail = 0

    n_cols = sum(len(s["columns"]) for s in tables.values())
    print(f"宣告 {len(tables)} 張新表 / {n_cols} 個欄位（尚未建立）\n")

    # ---- [1][2] 探針 -------------------------------------------------
    for level in ("live", "acknowledged"):
        probes = policy[level]["strings"]
        hits = []
        for probe in probes:
            p = probe.lower()
            for t, c, cc in cols:
                if p in c.lower() or p in (cc or "").lower():
                    hits.append((probe, f"{t}.{c}", cc))
        tag = "[1] 活探針      " if level == "live" else "[2] 已裁決探針  "
        if level == "live":
            if hits:
                fail += 1
                print(f"{tag}✗ 命中 {len(hits)} 處 —— 建表會讓閘門紅燈")
                for probe, where, cc in hits[:12]:
                    print(f"                  「{probe}」→ {where}  {cc}")
            else:
                print(f"{tag}OK（{len(probes)} 個探針全部沒命中）")
        else:
            if hits:
                print(f"{tag}⚠ 命中 {len(hits)} 處 —— 不擋，但要人確認沒讓防禦題實質可答")
                for probe, where, cc in hits[:12]:
                    print(f"                  「{probe}」→ {where}  {cc}")
            else:
                print(f"{tag}OK（{len(probes)} 個探針全部沒命中）")

    # ---- [2.5] 型別可用 ------------------------------------------------
    # 這一項是被咬出來的：YAML 的 flow mapping `{name: x, type: DECIMAL(6,1)}`
    # 會被型別裡的**逗號**切開，type 變成 `DECIMAL(6`，而稽核只看欄位名與數量
    # 的話完全看不出來 —— 直到 CREATE TABLE 才炸。
    # 「對照組與檢查工具本身也要驗」（ARCHITECTURE.md §8 ④）。
    _TYPE_OK = re.compile(
        r"^(INT|BIGINT|TINYINT|SMALLINT|DATE|DATETIME|TEXT|"
        r"VARCHAR\(\d+\)|DECIMAL\(\d+,\d+\))"
        r"( AUTO_INCREMENT PRIMARY KEY| NOT NULL)?$")
    bad_type = [f"{t}.{c['name']} = {c.get('type')!r}"
                for t, s in tables.items() for c in s["columns"]
                if not _TYPE_OK.match(str(c.get("type", "")).strip())]
    if bad_type:
        fail += 1
        print(f"[2.5] 型別可用    ✗ {len(bad_type)} 個欄位的型別不合法："
              f"（含逗號的型別要在 YAML 裡加引號）")
        for b in bad_type[:8]:
            print(f"                  {b}")
    else:
        print(f"[2.5] 型別可用    OK（{n_cols} 個欄位的型別都解得出來）")

    # ---- [3][4] 表名與掛載點 ------------------------------------------
    exist = existing_tables()
    dup = [t for t in tables if t in exist]
    if dup:
        fail += 1
        print(f"[3] 表名衝突    ✗ 已存在：{dup}")
    else:
        print(f"[3] 表名衝突    OK（現有 {len(exist)} 張表，新表名都沒撞到）")

    bad_attach = [f"{t}→{s['attach']['to']}" for t, s in tables.items()
                  if s["attach"]["to"] not in exist]
    if bad_attach:
        fail += 1
        print(f"[4] 掛載點存在  ✗ 找不到：{bad_attach}")
    else:
        print("[4] 掛載點存在  OK（每張都掛在現有主體上）")

    # ---- [5] 寬度 ------------------------------------------------------
    thin = {t: len(s["columns"]) for t, s in tables.items() if len(s["columns"]) < MIN_COLS}
    if thin:
        fail += 1
        print(f"[5] 寬度達標    ✗ 未達 {MIN_COLS} 欄：{thin}")
    else:
        widths = sorted(len(s["columns"]) for s in tables.values())
        print(f"[5] 寬度達標    OK（{widths[0]}~{widths[-1]} 欄，全部 ≥ {MIN_COLS}）")

    # ---- [6][7] 題目 ---------------------------------------------------
    qs = plan["questions"]
    covered = set()
    for q in qs:
        covered.update(q.get("tables") or [q["table"]] if q.get("table") else (q.get("tables") or []))
    missing = [t for t in tables if t not in covered]
    if missing:
        fail += 1
        print(f"[6] 題目覆蓋    ✗ 沒有配題的新表：{missing}")
    else:
        print(f"[6] 題目覆蓋    OK（{len(qs)} 題涵蓋全部 {len(tables)} 張新表）")

    roles = {k: 0 for k in QUOTA}
    for q in qs:
        for r in q.get("roles") or []:
            if r in roles:
                roles[r] += 1
    short = {k: f"{v}/{QUOTA[k]}" for k, v in roles.items() if v < QUOTA[k]}
    if short:
        fail += 1
        print(f"[7] 角色配額    ✗ 不足：{short}")
        print("                  配額不是形式 —— multi_col 不夠，"
              "eval_column_recall 就還是會回 100%，這次擴充等於白做")
    else:
        print(f"[7] 角色配額    OK（{', '.join(f'{k}={v}' for k, v in roles.items())}）")

    print()
    if fail:
        print(f"{'=' * 70}\n{fail} 項未通過 —— **不要建表**，先改 wide_table_plan.yaml")
        return 1
    print(f"{'=' * 70}\n七項全部通過：可以進到建表腳本。")
    print("建表時仍要守：只 CREATE + INSERT、專屬亂數種子、事後逐表 SHA 指紋比對。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
