"""防禦題稽核 —— 加表有沒有讓「這個概念不存在」的斷言過期？

為什麼需要這支（ARCHITECTURE.md §8.8、§9.1）：

    一般題斷言「答案是什麼」，正確性只依賴用到的那幾張表。
    防禦題斷言「概念不存在」，正確性依賴**整個 schema 的補集** ——
    所以加任何一張表都可能讓它失效，而且失效時是**靜默的**：
    系統答對了卻被判錯，看起來像模型退步。

    實例：加 customer_profiles 之後 #69（年齡）與 #71（性別比例）
    被 birth_year / gender 作廢，端到端誤報成兩題退步。

做法：每題防禦題在 eval_ground_truth.yaml 裡宣告 `absent:`，
列出「這題成立的前提是庫裡沒有這些概念」。本程式掃 INFORMATION_SCHEMA
的欄位名與欄位註解比對，命中就要人來裁決。

裁決判準（§8.8）是**「業務定義在不在」，不是「欄位在不在」**：

    #70 利潤率  supply_price 有了，但一商品多進貨價、公式未定義 → 維持防禦
    #69 年齡    birth_year 有了，2026 - birth_year 無歧義        → 改一般題

裁決完的命中寫進該概念的 `acknowledged:`，之後就不再報警 ——
**未裁決的命中才是警報**，這樣加表時只會看到新出現的東西。

用法：
    python tools/check_defence_gt.py          # 有未裁決命中時 exit 1
    python tools/check_defence_gt.py --all    # 連已裁決的也列出來
"""
import io
import os
import sys

import yaml
from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

GT_PATH = "eval_ground_truth.yaml"


def load_columns() -> list[tuple[str, str, str]]:
    """(表, 欄位, 註解) —— 直接讀 INFORMATION_SCHEMA，不經過任何快取或 YAML 副本。"""
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        return [(t, c, cc or "") for t, c, cc in conn.execute(text("""
            SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME), COLUMN_COMMENT
            FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """)).fetchall()]


def hits_for(probe: str, columns) -> list[str]:
    p = probe.lower()
    return [f"{t}.{c}（{cc}）" if cc else f"{t}.{c}"
            for t, c, cc in columns if p in c.lower() or p in cc.lower()]


def audit(gt: list[dict], columns) -> tuple[int, list[int], list[str]]:
    """回傳 (未裁決命中數, 沒宣告 absent 的題號, 給人看的說明行)。

    給 eval_score.py 共用 —— 端到端跑完自動稽核一次，
    因為防禦題失效是靜默的：系統答對了卻被判錯（§8.8）。
    """
    alarms, lines = 0, []
    defence = [e for e in gt if e.get("expect") == "schema_unsupported"]
    undeclared = [e["id"] for e in defence if not e.get("absent")]
    for entry in defence:
        for concept in entry.get("absent") or []:
            if concept.get("acknowledged"):
                continue
            found = []
            for probe in concept.get("probes") or []:
                for h in hits_for(probe, columns):
                    if h not in found:
                        found.append(h)
            if not found:
                continue
            alarms += 1
            lines.append(f"#{entry['id']} {entry['question']}")
            lines.append(f"    概念「{concept.get('concept', '?')}」有 "
                         f"{len(found)} 個欄位命中但尚未裁決：")
            for h in found[:8]:
                lines.append(f"      {h}")
    return alarms, undeclared, lines


VERDICT_HINT = (
    "依 §8.8 判準逐一裁決：業務定義在不在，不是欄位在不在。\n"
    "  仍然無定義 → 在該概念補 acknowledged: 說明為什麼維持防禦\n"
    "  定義已明確 → 把這題改成一般題並補 GT SQL"
)


def main() -> int:
    show_all = "--all" in sys.argv
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    columns = load_columns()
    defence = [e for e in gt if e.get("expect") == "schema_unsupported"]

    print(f"防禦題 {len(defence)} 題；全庫 {len(columns)} 個欄位\n")

    for entry in defence:
        lines: list[str] = []
        for concept in entry.get("absent") or []:
            name = concept.get("concept", "?")
            ack = (concept.get("acknowledged") or "").strip()
            found: list[str] = []
            for probe in concept.get("probes") or []:
                for h in hits_for(probe, columns):
                    if h not in found:
                        found.append(h)
            if not found:
                if show_all:
                    lines.append(f"    [OK ] {name}：無任何欄位命中")
                continue
            if ack:
                if show_all:
                    lines.append(f"    [已裁決] {name} 命中 {len(found)} 個欄位")
                    for h in found[:6]:
                        lines.append(f"             {h}")
                    lines.append(f"             → {ack.splitlines()[0]}")
                continue
            lines.append(f"    [警報] {name} —— 有欄位命中但尚未裁決")
            for h in found[:10]:
                lines.append(f"            {h}")
            if len(found) > 10:
                lines.append(f"            …共 {len(found)} 個")
        if lines or show_all:
            print(f"#{entry['id']} {entry['question']}")
            print("\n".join(lines) or "    （全部通過）")
            print()

    alarms, undeclared, _ = audit(gt, columns)
    if undeclared:
        print(f"⚠️  這些防禦題還沒宣告 absent:，稽核不到 → {undeclared}\n")

    print("=" * 70)
    if alarms or undeclared:
        print(f"未裁決命中 {alarms} 個、未宣告 {len(undeclared)} 題。")
        print(VERDICT_HINT)
        return 1
    print("全部通過：沒有新的 schema 讓防禦題的前提失效。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
