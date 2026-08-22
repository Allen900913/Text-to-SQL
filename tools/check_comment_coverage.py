"""表註解涵蓋率稽核 —— 欄位裡有的概念，表註解漏了哪些？（離線 LLM）

與 `check_comment_blindspot.py` 的分工（兩支都要，因為它們看的方向相反）：

    blindspot   **問句驅動**：GT SQL 說哪張表才對，就檢查那張表的註解有沒有
                出現問句的字面詞。看得到「這一題會不會漏」，
                看不到「還沒被問到的概念」。
    coverage    **欄位驅動**（本支）：把 52 個欄位註解讀完，歸納出這張表實際
                承載哪些概念群，逐一比對表註解有沒有涵蓋。
                **不需要題目**，所以它抓得到「將來會冤枉的題」。

為什麼需要後者 —— 2026-08-22 的實測：

    `#263`「被系統擋下來要人工看過的單子」8/8 全錯（5 次誤觸防禦暗號）。
    `order_profiles` 有四個 `fraud_review_*` 欄位，而表註解一個字都沒提到
    風控覆核 —— 那**整組概念**在檢索文件與候選目錄裡都不存在。
    blindspot 沒有報這一題（它報的 `#277`/`#278` 實測都對），
    因為問句寫的是口語的「擋下來、人工看過」，字面上跟註解沒得比。

    這就是分工的意義：**字面比對抓誘餌，概念歸納抓盲區。**（§2.5 結論 3）

輸出是**候選清單，不是處方**。判準與 §2.5 的六步一致：
  · 要補的是**概念家族**，不是某一題的關鍵詞（避免 teaching to the test）
  · 補完必跑稀釋鏡像檢查 —— 一張表一句註解有容量上限（3~4 個概念群），
    塞第五個概念會讓它在更多不相干的問題上排第一
  · 唯一來源是 `db/init_db*.py` 或 `tools/*_plan.yaml`，不是資料庫

用法：
    python tools/check_comment_coverage.py                 # 全庫
    python tools/check_comment_coverage.py --min-cols 12   # 只看寬表（預設 8）
    python tools/check_comment_coverage.py --only order_profiles product_profiles
"""
import argparse
import json
import os
import re
import sys

from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.config import MYSQL_URI, llm_fast  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

PROMPT = """你在稽核一個繁體中文 Text-to-SQL 系統的資料表註解。

表註解是這張表唯一的檢索訊號：使用者用中文問問題，系統拿問題去跟表註解做語意
比對，決定要不要把這張表送進 Prompt。**欄位註解不會進檢索。**
所以只要某個概念只寫在欄位註解裡、沒寫進表註解，問那個概念的問題就永遠找不到
這張表 —— 而且不會報錯，只會靜默答錯或誠實拒答。

你的工作：讀完全部欄位註解，歸納這張表實際承載哪些**概念群**，
然後逐一判斷表註解有沒有涵蓋它。

表名：{table}
現在的表註解：{comment}

欄位（名稱 -- 註解）：
{columns}

請只輸出一個 JSON 物件，不要有其他文字、不要用程式碼圍欄：

{{"groups": [
  {{"name": "概念群名稱（4~10 個中文字）",
    "cols": ["代表欄位1", "代表欄位2"],
    "covered": true 或 false,
    "why": "covered=false 時說明表註解缺什麼；covered=true 時寫涵蓋它的那幾個字"}}
]}}

判準（很重要）：
1. 概念群是**業務概念**，不是欄位型別分組。「風控覆核」「消防與租約」是概念群；
   「日期欄位」「布林旗標」不是。
2. covered 判斷的是**語意涵蓋**，不是字面相同。表註解寫「客服接觸紀錄」時，
   `support_contact_count` 算涵蓋；但寫「客服接觸」不代表涵蓋「風控覆核」——
   兩者是不同的業務動作，使用者問其中一個時不會聯想到另一個。
3. 主鍵、外鍵、`created_at` 這類結構欄位不用歸群。
4. 一張表通常有 3~7 個概念群。**不要為了湊數把一個概念拆成兩個。**
"""


def load_tables(conn, min_cols: int, only: list[str]):
    rows = conn.execute(text("""
        SELECT LOWER(c.TABLE_NAME), t.TABLE_COMMENT, c.COLUMN_NAME, c.COLUMN_COMMENT
        FROM INFORMATION_SCHEMA.COLUMNS c
        JOIN INFORMATION_SCHEMA.TABLES t
          ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
        WHERE c.TABLE_SCHEMA = DATABASE()
        ORDER BY c.TABLE_NAME, c.ORDINAL_POSITION
    """)).fetchall()
    out: dict[str, dict] = {}
    for table, tc, col, cc in rows:
        e = out.setdefault(table, {"comment": tc or "", "cols": []})
        e["cols"].append((col, cc or ""))
    if only:
        return {t: v for t, v in out.items() if t in {o.lower() for o in only}}
    return {t: v for t, v in out.items() if len(v["cols"]) >= min_cols}


def ask(table: str, info: dict) -> list[dict]:
    cols = "\n".join(f"  {c} -- {m}" if m else f"  {c}" for c, m in info["cols"])
    prompt = PROMPT.format(table=table, comment=info["comment"] or "（無）", columns=cols)
    raw = llm_fast.invoke(prompt).content.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.M).strip()
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError(f"回傳不是 JSON：{raw[:120]}")
    return json.loads(m.group(0)).get("groups", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-cols", type=int, default=8, help="只稽核欄位數 >= N 的表")
    ap.add_argument("--only", nargs="*", default=[], help="只稽核指定的表")
    args = ap.parse_args()
    log.remove()

    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        tables = load_tables(conn, args.min_cols, args.only)

    print(f"稽核 {len(tables)} 張表（欄位數 >= {args.min_cols}）\n")
    gaps: list[tuple[str, dict]] = []
    for i, (table, info) in enumerate(sorted(tables.items()), 1):
        try:
            groups = ask(table, info)
        except Exception as exc:
            print(f"[{i:3d}/{len(tables)}] {table:26s} ⚠ {type(exc).__name__}: {exc}")
            continue
        missing = [g for g in groups if not g.get("covered")]
        flag = f"✗ 漏 {len(missing)}" if missing else "OK"
        print(f"[{i:3d}/{len(tables)}] {table:26s} {len(info['cols']):3d} 欄 / "
              f"{len(groups)} 群  {flag}")
        for g in missing:
            gaps.append((table, g))

    print("\n" + "=" * 74)
    if not gaps:
        print("每一張表的概念群都寫進表註解了。")
        return 0

    print(f"{len(gaps)} 個概念群只存在於欄位註解裡，表註解沒提到：\n")
    for table, g in gaps:
        print(f"  {table}.{g['name']}")
        print(f"      欄位：{'、'.join(g.get('cols', [])[:6])}")
        print(f"      缺口：{g.get('why', '')}")

    print("\n" + "=" * 74)
    print("這是**候選清單，不是處方**（§2.5 六步）：\n"
          "  · 補概念家族，不補某一題的關鍵詞\n"
          "  · 一張表一句註解只塞得下 3~4 個概念群 —— 補第五個之前先想清楚要捨棄哪個\n"
          "  · 改唯一來源（db/init_db*.py 或 tools/*_plan.yaml），"
          "再 sync_table_comments.py --apply\n"
          "  · 補完必跑稀釋鏡像檢查 + eval_stability（目標題 + 同表其他題）")
    return 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
