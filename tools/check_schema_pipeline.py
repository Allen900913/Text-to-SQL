"""schema 改動之後，該同步的東西有沒有同步？—— 加表前的安全網

為什麼需要這支（ARCHITECTURE.md §11.1）：

    2026-08 這一輪連續被同一類 bug 咬了三次，三次都**不會報錯**：

      ① 19 張表建好之後 `gen_ddl.py` 從沒重跑 → DDL 卡在 22 張表。
         檢索選對了 `invoices`，生成端看不到欄位，於是誠實回「schema 不支援」。
         在對帳表上長得跟一個正確的防禦題**一模一樣**。
      ② `sync_table_comments.py` 的 SOURCES 漏了 `add_distractor_tables.py`，
         那 19 張表的註解沒有任何同步路徑；而且它只同步表註解不同步欄位註解。
      ③ 表級 COMMENT 進 DDL 之後，`schema_parser` 的 regex `\\);` 一個區塊都
         對不上，直接回空 dict —— 它是 MySQL 連不上時的降級來源。

    共同形狀：**新增了一個 schema 來源，卻沒有把它接到既有的產生鏈上。**
    `_report_drift()` 其實整輪都在警告，但它只 warning 不阻斷 ——
    **只警告不阻斷的檢查，等於沒有檢查，除非有人會讀。**

    所以這支的預設行為是 exit 1。加表之前跑它，紅了就不要往下走。

八項檢查：
  1. DDL 覆蓋      semantic_layer 的 ddl 是否含全庫每一張表
  2. DDL 新鮮度    ddl 與 gen_ddl.py 現在會產生的內容是否逐字相同
  3. 註解同步      表註解與欄位註解是否與 init 腳本的宣告一致
  4. regex 可解    schema_parser 的 DDL 剖析結果是否等於 INFORMATION_SCHEMA
  5. KMB 成本      每張表是否都做過成本決定（§11.2）
  6. 干擾表計畫    每張表是否都在 seed plan 裡有交代
  7. 覆蓋與宣告    零 GT 覆蓋的表是否都標了 never_answered
  8. prompt 鏈     semantic_layer.yaml 寫的欄位是否真的進得了 Prompt

用法：
    python tools/check_schema_pipeline.py           # 有問題就 exit 1
    python tools/check_schema_pipeline.py --fix     # 能自動修的先修（DDL、註解）
"""
import io
import os
import subprocess
import sys

import yaml
from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.schema_graph import unreviewed_tables  # noqa: E402
from langgraph_sql.utils.schema_parser import get_schema_parser  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
GT_PATH = os.path.join(ROOT, "eval_ground_truth.yaml")
PLAN_PATH = os.path.join(HERE, "distractor_seed_plan.yaml")


def live_tables(db) -> set[str]:
    with db.engine.connect() as conn:
        return {r[0].lower() for r in conn.execute(text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"))}


def live_columns(db) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    with db.engine.connect() as conn:
        for t, c in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()")):
            out.setdefault(t.lower(), set()).add(c.lower())
    return out


def gt_covered() -> set[str]:
    """GT SQL 實際參照到的表。"""
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    used: set[str] = set()
    for e in gt:
        for s in [e.get("sql") or ""] + list(e.get("alt_sql") or []):
            try:
                for t in sqlglot.parse_one(s, dialect="mysql").find_all(exp.Table):
                    used.add(t.name.lower())
            except Exception:
                pass
    return used


def run(script: str, *args: str) -> tuple[int, str]:
    """跑同目錄下的另一支工具，回傳 (exit code, 輸出)。"""
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def longest_line(value) -> str:
    """挑一段最有辨識度的字當探針 —— 最長的那一非空行。"""
    lines = [ln.strip() for ln in str(value).splitlines() if ln.strip()]
    return max(lines, key=len) if lines else ""


def prompt_reach(parser) -> list[str]:
    """semantic_layer.yaml 裡寫的東西，有沒有真的進得了 Prompt？

    為什麼需要這一項（ARCHITECTURE.md §11.9）：
        `get_few_shot_text()` 只送 question + SQL —— **24 則 few_shot 的
        `reasoning` 從來沒有進過 Prompt**，4,649 字元全是死的。
        `get_rules_text()` 早就為 business_rules 修過同一個 bug，few-shot 漏了。
        於是「以後都從 few-shot 解決」這條方針在結構上一直做不到，
        而且過去每一次「加了 few-shot 沒效果」的觀察都被這個 bug 污染。

    前七項守的是 **schema 鏈**（DB → DDL → parser），這一項守的是
    **prompt 鏈**（YAML → renderer → Prompt）。同樣是「寫了卻沒接上」，
    同樣不會報錯 —— 差別只在 YAML 有欄位很容易讓人以為它有效。

    做法：每個欄位挑最長的一行當探針，看它在不在 renderer 的輸出裡。
    不比對完整內容 —— renderer 本來就會加縮排與前綴，比對整串會一直假警報。
    """
    bad: list[str] = []
    sections = (
        ("few_shot_examples", parser.get_few_shot_text(),
         ("question", "reasoning", "sql", "sql_step1", "sql_step2", "wrong_sql")),
        ("business_rules", parser.get_rules_text(),
         ("name", "description", "correct_sql", "wrong_sql")),
    )
    for key, rendered, fields in sections:
        for i, entry in enumerate(parser._data.get(key) or []):
            label = str(entry.get("question") or entry.get("name") or i)[:28]
            for field in fields:
                if not entry.get(field):
                    continue
                probe = longest_line(entry[field])
                if probe and probe not in rendered:
                    bad.append(f"{key}[{label}].{field}")

    enum_text = parser.get_enum_text()
    for field in (parser._data.get("enum_fields") or {}):
        if field not in enum_text:
            bad.append(f"enum_fields[{field}]")
    return bad


def main() -> int:
    fix = "--fix" in sys.argv
    db = get_db_manager(MYSQL_URI)
    tables = live_tables(db)
    cols = live_columns(db)
    parser = get_schema_parser()
    problems: list[str] = []

    print(f"資料庫 {len(tables)} 張表 / {sum(len(v) for v in cols.values())} 個欄位\n")

    # --- 1 & 2. DDL 覆蓋與新鮮度 -------------------------------------------
    if fix:
        # 用子行程跑，寫回 YAML 之後這個行程裡的 parser 是舊快取 ——
        # 所以 --fix 之後要再跑一次不帶 --fix 的檢查才算數，最後會提示。
        run("gen_ddl.py", "--write")

    ddl_tables = {t.lower() for t in parser.get_allowed_tables()}
    missing_ddl = sorted(tables - ddl_tables)
    if missing_ddl:
        problems.append(
            f"[1] DDL 少了 {len(missing_ddl)} 張表 → {missing_ddl}\n"
            f"      這些表檢索選得到、生成端卻看不到欄位，症狀是「誤觸防禦暗號」。\n"
            f"      修法：python tools/gen_ddl.py --write")
    else:
        print(f"[1] DDL 覆蓋      OK（{len(ddl_tables)} 張表）")

    from tools.gen_ddl import build_ddl  # noqa: E402  匯入時會連 DB，放這裡
    fresh = build_ddl().strip()
    if fresh != parser.get_ddl().strip():
        problems.append(
            "[2] DDL 不是最新的 —— gen_ddl.py 現在會產生不同的內容。\n"
            "      通常代表有人改了 init 腳本的註解卻沒重新產生。\n"
            "      修法：python tools/gen_ddl.py --write")
    else:
        print("[2] DDL 新鮮度    OK")

    # --- 3. 註解同步 --------------------------------------------------------
    if fix:
        run("sync_table_comments.py", "--apply")
    code, out = run("sync_table_comments.py")
    if "都已一致" not in out:
        tail = "\n      ".join(out.strip().splitlines()[-6:])
        problems.append(f"[3] 註解未同步：\n      {tail}\n"
                        f"      修法：python tools/sync_table_comments.py --apply")
    else:
        print("[3] 註解同步      OK")

    # --- 4. regex 可解 ------------------------------------------------------
    parsed = {k.lower(): {c.lower() for c in v}
              for k, v in parser.get_table_columns().items()}
    if parsed != cols:
        only_db = sorted(set(cols) - set(parsed))
        only_ddl = sorted(set(parsed) - set(cols))
        diff_cols = sorted(t for t in set(cols) & set(parsed) if cols[t] != parsed[t])
        problems.append(
            f"[4] schema_parser 的 DDL regex 剖析結果與 INFORMATION_SCHEMA 不符\n"
            f"      只在 DB: {only_db}\n      只在 DDL: {only_ddl}\n"
            f"      欄位不同: {diff_cols}\n"
            f"      這支是 MySQL 連不上時的降級來源，壞掉不會有人喊（§10.7）。")
    else:
        print("[4] regex 可解    OK")

    # --- 5. KMB 成本 --------------------------------------------------------
    unreviewed = unreviewed_tables(tables)
    if unreviewed:
        problems.append(
            f"[5] 這些表沒做過 KMB 成本決定 → {unreviewed}\n"
            f"      行為／紀錄類的表（瀏覽、收藏、加購物車）要調高成本，\n"
            f"      否則 KMB 會走「拓撲最短但語意錯」的路，而且不會報錯。\n"
            f"      修法：在 langgraph_sql/utils/schema_graph.py 把它加進\n"
            f"            _TABLE_COST（要調高）或 _DEFAULT_COST_REVIEWED（維持 1）。")
    else:
        print("[5] KMB 成本      OK（每張表都做過決定）")

    # --- 6 & 7. 干擾表計畫與覆蓋宣告 ----------------------------------------
    plan = yaml.safe_load(io.open(PLAN_PATH, encoding="utf-8")) or {}
    covered = gt_covered()
    uncovered = sorted(tables - covered)
    undeclared = [t for t in uncovered if not (plan.get(t) or {}).get("never_answered")]
    if undeclared:
        problems.append(
            f"[6] 這些表沒有任何 GT 覆蓋，也沒宣告 never_answered → {undeclared}\n"
            f"      零覆蓋的表對檢索器等於「永遠別選就對了」的捷徑（§10.1）。\n"
            f"      修法：補題，或在 seed plan 標 never_answered: true 並說明理由。")
    else:
        print(f"[6] 覆蓋與宣告    OK（覆蓋 {len(tables & covered)}/{len(tables)}，"
              f"永不作答 {len(uncovered)} 張且皆已宣告）")

    empty = []
    with db.engine.connect() as conn:
        for t in sorted(tables):
            if conn.execute(text(f"SELECT COUNT(*) FROM `{t}`")).scalar() == 0:
                empty.append(t)
    if empty:
        problems.append(
            f"[7] 空表 → {empty}\n"
            f"      零列的表會讓 expect: empty 的題目「選錯表也判對」，\n"
            f"      而且讓熱度／值檢索這類方法假性滿分（§7.10）。")
    else:
        print("[7] 沒有空表      OK")

    # --- 8. prompt 鏈 -------------------------------------------------------
    unreached = prompt_reach(parser)
    if unreached:
        shown = "\n      ".join(unreached[:12])
        more = "\n      …" if len(unreached) > 12 else ""
        problems.append(
            f"[8] 這些 YAML 欄位寫了、卻進不了 Prompt（{len(unreached)} 個）→\n"
            f"      {shown}{more}\n"
            f"      前七項守 schema 鏈，這一項守 prompt 鏈：YAML → renderer → Prompt。\n"
            f"      實例：few-shot 的 reasoning 曾經整整 24 則都沒送出去（§11.9），\n"
            f"      害「加了 few-shot 沒效果」這個觀察被污染了不知道多久。\n"
            f"      修法：改 langgraph_sql/utils/schema_parser.py 的對應 renderer。")
    else:
        print(f"[8] prompt 鏈     OK（few-shot {len(parser.get_few_shot_text())} 字元 / "
              f"rules {len(parser.get_rules_text())} 字元，每個欄位都送得出去）")

    print()
    if problems:
        print("=" * 70)
        print(f"{len(problems)} 項未通過 —— **加表之前不要往下走**\n")
        print("\n\n".join(problems))
        print("=" * 70)
        if not fix:
            print("其中 DDL 與註解可以自動修：python tools/check_schema_pipeline.py --fix")
        else:
            print("已跑過 --fix。請**再跑一次不帶 --fix 的檢查**確認 —— "
                  "這個行程裡的 parser 讀的還是修改前的快取。")
        return 1
    print("=" * 70)
    print("八項全部通過：schema 與 prompt 兩條鏈的下游都跟上了。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
