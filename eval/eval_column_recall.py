"""欄位級召回 —— 欄位裁剪上線前唯一的守門員

為什麼需要這支（ARCHITECTURE.md §2.15~§2.18、§9.4）：

    表沒給   → EXPLAIN 報錯 / 空結果          會叫，看得見
    欄位沒給 → 模型挑一個看起來像的，
               SQL 合法、EXPLAIN 過、結果錯    不會叫

    整條 pipeline 上，欄位裁剪是唯一沒有下游守門員的環節。所以它必須有一個
    **獨立於端到端正確率**的指標，否則正確率掉了也無從歸因。
    這份 ground truth 是免費的：GT SQL 用到哪些欄位，就是那題至少要看到的。

現況是**不裁**（§2.17：省 37~99 字元，而每題 DDL 是 1,041 字元 ——
省不到東西的時候才安全）。這支程式有兩個用途：真要裁的那一天，先在這裡拿到
100% 欄位召回再上線；以及 schema 變寬之後，重新檢查那個「不裁」的結論還成不成立。

被測的裁剪器就是 §2.16 量出來的形狀：

    1. 鍵（PK/FK）一律保留 —— 結構性資訊不能交給語意評分決定。
       固定 Top-5 漏掉的欄位有 71% 是 JOIN 鍵，而 customers.id 跟任何
       中文問題都沒有語意相似，它的必要性是結構性的。
    2. 非鍵欄位按「對問題的餘弦相似度」取 Top-K。
    3. 寬度閘門 W：只裁欄位數 > W 的表。窄表裁不出東西卻要承擔同樣的風險。

用法：
    python eval/eval_column_recall.py                        # 掃 K 與 W 的組合
    python eval/eval_column_recall.py --k 4 --w 0            # 檢查單一設定，未達標 exit 1
    python eval/eval_column_recall.py --k 4 --w 12 --verbose # 列出漏掉的欄位
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

import argparse  # noqa: E402
import io  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from eval_schema_need import GT_PATH, required_schema  # noqa: E402
from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.schema_registry import get_table_columns  # noqa: E402
from langgraph_sql.utils.table_retriever import _cosine, _embed  # noqa: E402

VEC_CACHE = _os.path.join(_ROOT, ".column_vectors.json")
CHARS_PER_COL = 65  # 實測 DDL 每欄位約 65 字元


def load_meta():
    """(鍵集合, 欄位註解, 每張表的欄位) —— 一律以 INFORMATION_SCHEMA 為準。"""
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        meta = conn.execute(text("""
            SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME), COLUMN_KEY, COLUMN_COMMENT
            FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
            ORDER BY TABLE_NAME, ORDINAL_POSITION
        """)).fetchall()
        fks = {f"{t}.{c}" for t, c in conn.execute(text("""
            SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME)
            FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
            WHERE TABLE_SCHEMA = DATABASE() AND REFERENCED_TABLE_NAME IS NOT NULL
        """)).fetchall()}
    keys = {f"{t}.{c}" for t, c, k, _ in meta if k in ("PRI", "MUL")} | fks
    comments = {f"{t}.{c}": (cc or "") for t, c, _, cc in meta}
    cols_of: dict[str, list[str]] = {}
    for t, c, _, _ in meta:
        cols_of.setdefault(t, []).append(c)
    return keys, comments, cols_of


def column_vectors(comments, cols_of):
    """欄位向量走自己的磁碟快取 —— 這是離線評估用的，不進 production 檢索。"""
    docs = {f"{t}.{c}": (f"{t}.{c}（{comments[f'{t}.{c}']}）"
                         if comments.get(f"{t}.{c}") else f"{t}.{c}")
            for t in cols_of for c in cols_of[t]}
    cache = {}
    if os.path.exists(VEC_CACHE):
        cache = json.load(io.open(VEC_CACHE, encoding="utf-8"))
    todo = sorted(set(docs.values()) - set(cache))
    if todo:
        print(f"嵌入 {len(todo)} 個新的欄位文件…")
        for vec, doc in zip(_embed(todo, "passage"), todo):
            cache[doc] = vec
        json.dump(cache, io.open(VEC_CACHE, "w", encoding="utf-8"))
    return {name: cache[doc] for name, doc in docs.items()}


def prune(table, qvec, keys, cols_of, vecs, k: int, w: int) -> set[str]:
    """回傳這張表裁剪後保留的欄位。寬度閘門沒選中的表原封不動。"""
    cols = cols_of[table]
    if len(cols) <= w:
        return set(cols)
    nonkey = [c for c in cols if f"{table}.{c}" not in keys]
    nonkey.sort(key=lambda c: (-_cosine(qvec, vecs[f"{table}.{c}"]), c))
    return {c for c in cols if f"{table}.{c}" in keys} | set(nonkey[:k])


def evaluate(cases, qvecs, keys, cols_of, vecs, k, w, verbose=False):
    full = got = tot = kept = allc = 0
    misses = []
    for (qid, question, tabs, need), qvec in zip(cases, qvecs):
        ok = True
        for t in tabs:
            if t not in cols_of:
                continue
            keep = prune(t, qvec, keys, cols_of, vecs, k, w)
            kept += len(keep)
            allc += len(cols_of[t])
            for nc in (c for c in need if c.startswith(f"{t}.")):
                tot += 1
                if nc.split(".", 1)[1] in keep:
                    got += 1
                    continue
                ok = False
                if verbose:
                    misses.append(f"  #{qid:<4} 漏 {nc}"
                                  f"{'  [JOIN鍵]' if nc in keys else ''}"
                                  f"  {question[:30]}")
        full += ok
    saved = (allc - kept) * CHARS_PER_COL / len(cases)
    return full / len(cases), got / tot, 1 - kept / allc, saved, misses


def load_cases(known):
    """只有 GT SQL 解得出表與欄位的題目才可評估（防禦題沒有 GT SQL）。"""
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    cases = []
    for e in gt:
        if not e.get("sql"):
            continue
        try:
            tabs, cols = required_schema(e["sql"], known)
        except Exception:
            continue
        if tabs and cols:
            cases.append((e["id"], e["question"], tabs, cols))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--k", type=int, help="非鍵欄位取 Top-K")
    ap.add_argument("--w", type=int, help="寬度閘門：只裁欄位數 > W 的表")
    ap.add_argument("--verbose", action="store_true", help="列出漏掉的欄位")
    args = ap.parse_args()

    log.remove()
    keys, comments, cols_of = load_meta()
    cases = load_cases(get_table_columns())
    vecs = column_vectors(comments, cols_of)
    qvecs = _embed([q for _, q, _, _ in cases], "query")

    total_cols = sum(len(v) for v in cols_of.values())
    print(f"可評估 {len(cases)} 題｜全庫 {len(cols_of)} 張表 / {total_cols} 欄，"
          f"其中鍵 {len(keys)} 個（{len(keys) / total_cols:.0%}）\n")

    if args.k is not None:
        w = args.w if args.w is not None else 0
        full, recall, ratio, saved, misses = evaluate(
            cases, qvecs, keys, cols_of, vecs, args.k, w, args.verbose)
        gated = sum(1 for t in cols_of if len(cols_of[t]) > w)
        print(f"設定 K={args.k} W={w}（{gated}/{len(cols_of)} 張表會被裁）")
        print(f"  題目欄位全中  {full:.1%}")
        print(f"  欄位召回      {recall:.1%}")
        print(f"  省下欄位      {ratio:.1%}（平均每題 {saved:.0f} 字元）")
        if misses:
            print("\n漏掉的欄位:")
            print("\n".join(misses[:40]))
        if recall < 1.0:
            print("\n未達標：欄位召回不是 100%，這個設定不可上線。")
            print("      失敗是靜默的（§2.15）—— 模型不會報錯，只會挑個相近的湊。")
            return 1
        print("\n達標：這個設定不會漏掉任何 GT 需要的欄位。")
        print("      但先讀 §2.17 —— 目前的結論是不要裁，省下的字元不值這個風險。")
        return 0

    print(f"{'K':>3} {'W':>4}  {'會裁的表':>8}  {'欄位全中':>8}  {'欄位召回':>8}"
          f"  {'省欄位':>7}  {'每題省':>9}")
    print("-" * 64)
    for w in (0, 8, 12, 20):
        for k in (2, 3, 4, 5):
            full, recall, ratio, saved, _ = evaluate(
                cases, qvecs, keys, cols_of, vecs, k, w)
            gated = sum(1 for t in cols_of if len(cols_of[t]) > w)
            flag = "" if recall == 1.0 else "  ← 會漏"
            print(f"{k:>3} {w:>4}  {gated:>8}  {full:>7.1%}  {recall:>7.1%}"
                  f"  {ratio:>6.1%}  {saved:>6.0f} 字元{flag}")
        print()
    print("判讀：欄位召回不是 100% 的設定一律不可上線 —— 欄位漏給不會叫。")
    print("      而達標的設定省下的字元通常也很少，那正是 §2.17 不做的理由。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
