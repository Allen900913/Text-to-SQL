"""僥倖偵測 —— 答案對，但用的 schema 不對

為什麼需要這支（ARCHITECTURE.md §6.1、§9.2）：

    #62「單價超過 30000 元商品」檢索漏掉 products，模型改用
    order_items.unit_price（成交價）而 GT 用 products.price（售價）。
    兩者語意不同，只因為種子資料裡這兩欄**永遠相等**才回傳同一組答案，
    端到端把它計為「對」。

    也就是說：**端到端正確率會低報檢索失敗** —— 表沒給對時模型會用手上的
    表湊一個形狀相近的答案，而那個答案有機會在現有資料上剛好對。

做法：對每一題判定為 correct 的結果，用 sqlglot 解出模型 SQL 實際用到的
表與欄位，和 GT SQL 的需求比對。不一致就標成**僥倖候選**。

這是**旗標不是判決** —— 同一個問題本來就有多種正確寫法（`#36` 的
DATE_FORMAT vs YEAR/MONTH 就是），所以只要模型的 schema 涵蓋 GT 或任何
一條 alt_sql 的需求就算正常。剩下的要人看。

`eval_score.py` 會在對帳時自動呼叫這裡的 `audit_one()`，所以正常流程
不需要單獨跑這支；要單看稽核結果或加 --columns 時才用得到。

用法：
    python eval/eval_lucky.py                       # 取最新的 eval_result_*.json
    python eval/eval_lucky.py eval_result_X.json
    python eval/eval_lucky.py --columns             # 連欄位級的差異也列出來
"""
import os as _os
import sys as _sys

# 搬進子目錄之後要自己把專案根目錄放進 sys.path（tools/ 也是這個寫法）。
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_RESULTS = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "results")

import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")

import glob  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402

from eval_schema_need import required_schema  # noqa: E402
from eval_score import GT_PATH, judge  # noqa: E402
from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402


def _schema_of(sql: str, known) -> tuple[set[str], set[str]] | None:
    try:
        return required_schema(sql, known)
    except Exception:
        return None  # 解析不了就不稽核，寧可漏報也不要誤報


def audit_one(entry: dict, result: dict, known, verdict: str,
              show_cols: bool = False) -> tuple[str | None, str | None]:
    """回傳 (檢索漏給的說明, 僥倖候選的說明)，沒有就是 None。

    兩者互相獨立：
      檢索漏給   與答對與否無關，是 §5.3 的獨立指標
      僥倖候選   只看判定為 correct 的題（答錯的本來就會被列出來）
    """
    if not entry.get("sql"):
        return None, None  # 防禦題沒有 GT SQL
    sys_sql = (result.get("sql") or "").strip()
    if not sys_sql:
        return None, None

    wanted = [w for w in (_schema_of(s, known)
                          for s in [entry["sql"]] + list(entry.get("alt_sql") or []))
              if w]
    got = _schema_of(sys_sql, known)
    if not wanted or not got:
        return None, None

    miss = None
    retrieved = {t.lower() for t in (result.get("retrieved_tables") or [])}
    if retrieved:
        # 用需求最少的那條寫法當基準 —— 只要有一種正常寫法拿得到就不算漏
        easiest = min((w[0] for w in wanted), key=len)
        if not easiest <= retrieved:
            miss = (f"少給 {sorted(easiest - retrieved)}｜"
                    f"給了 {sorted(retrieved)}｜{entry['question'][:32]}")

    if verdict != "correct":
        return miss, None

    if not any(w[0] <= got[0] for w in wanted):
        best = min(wanted, key=lambda w: len(w[0] - got[0]))
        return miss, (f"[表] GT 要 {sorted(best[0])}，模型只用 {sorted(got[0])}\n"
                      f"       {entry['question'][:40]}\n       {sys_sql[:150]}")

    if show_cols and not any(w[1] <= got[1] for w in wanted):
        best = min(wanted, key=lambda w: len(w[1] - got[1]))
        return miss, (f"[欄位] 答對但少用 {sorted(best[1] - got[1])}\n"
                      f"       {entry['question'][:40]}\n       {sys_sql[:150]}")
    return miss, None


def report(misses: list[str], lucky: list[str]) -> None:
    """共用的輸出格式 —— eval_score.py 也用這一份。"""
    if misses:
        print(f"檢索漏給（{len(misses)} 題，與答案對錯無關）:")
        for m in misses:
            print(f"  {m}")
        print()
    if lucky:
        print(f"⚠️  僥倖候選（{len(lucky)} 題，答案對但 schema 不符，需人工複核）:")
        for m in lucky:
            print(f"  {m}")
        print("\n  這是旗標不是判決。同一題可以有多種正確寫法 ——")
        print("  確認是真的僥倖之後，補一條 alt_sql 或修 GT，不要直接改判。")
    elif not misses:
        print("檢索與 schema 稽核：全部通過。")


def main() -> int:
    log.remove()
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    show_cols = "--columns" in sys.argv
    path = args[0] if args else sorted(glob.glob(_os.path.join(_RESULTS, "eval_result_*.json")))[-1]
    print(f"稽核檔案: {path}\n")

    gt = {e["id"]: e for e in yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))}
    results = {int(r["id"]): r for r in json.load(io.open(path, encoding="utf-8"))}
    known = get_table_columns()
    db = get_db_manager(MYSQL_URI)

    misses, lucky, n = [], [], 0
    for qid in sorted(gt):
        result = results.get(qid)
        if result is None:
            continue
        verdict, _ = judge(db, gt[qid], result)
        miss, luck = audit_one(gt[qid], result, known, verdict, show_cols)
        if miss or luck:
            n += 1
        if miss:
            misses.append(f"#{qid:<4} {miss}")
        if luck:
            lucky.append(f"#{qid:<4} {luck}")

    print("=" * 72)
    report(misses, lucky)
    return 0


if __name__ == "__main__":
    sys.exit(main())
