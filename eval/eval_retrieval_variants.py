"""檢索層變體對照 —— A / C / D 三個方案在 dense 層的實測（零 LLM 呼叫）

背景（ARCHITECTURE.md §7.9）：41 張表這一輪，四題「最終 prompt 漏表」的表
相似度排名是 2 / 2 / 4 / 10 —— **相似度給了，LLM 沒選**。所以要問的是：
有沒有辦法在 dense 這一層就把正確的表推得更前面、把干擾表壓下去，
讓 LLM 那一關更好過。

三個變體（都不打 LLM，所以可以隨便重跑）：

  A 熱度／列數先驗   照 LinkedIn SQL Bot：候選檢索「以存取熱度過濾」。
                     這裡沒有 query log，用**列數**當熱度的代理。
  C 值／實體檢索     照 CHESS：從問題抽關鍵字，在**實際的欄位值**裡找，
                     命中就把那張表加分。近義表用文字分不開，用資料分得開。
  D 欄位優先並聯     照 RSL-SQL / bidirectional retrieval：除了「問題 vs 表」，
                     再算一次「問題 vs 欄位」，把高分欄位所屬的表併進來。
                     這條專打寬表盲點 —— email_verified_at 是欄位，
                     塞不進一句表註解（§8.7）。

⚠️ 這份實驗只有在干擾表**有資料**時才有意義。零列的干擾表會讓 A 與 C
   假性滿分（不是方法有效，是靶子是空的）——先跑 tools/seed_distractor_data.py。

指標三個一起看，缺一不可（§5.3）：
  候選召回@K   需要的表全部落在前 K 名的題數比例 —— 硬指標
  Top-1 命中   第 1 名是不是「需要的表」—— ∪Top-1 那道保險吃這個數字
  干擾搶 Top-1 干擾表搶到第 1 名的題數 —— §7.9 量到的汙染通道

用法：
    python eval/eval_retrieval_variants.py                 # 全部變體
    python eval/eval_retrieval_variants.py --only A C      # 只跑指定的
"""
import os as _os
import sys as _sys

# 搬進子目錄之後要自己把專案根目錄放進 sys.path（tools/ 也是這個寫法）。
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_RESULTS = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "results")

import io
import os
import sys
from collections import defaultdict

import yaml
from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from eval_column_recall import column_vectors, load_meta  # noqa: E402
from eval_schema_need import required_schema  # noqa: E402
from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.schema_registry import (  # noqa: E402
    get_schema_parser, get_table_columns,
)
from langgraph_sql.utils.table_retriever import (  # noqa: E402
    _cosine, _embed, get_table_vectors,
)

GT_PATH = _os.path.join(_ROOT, "eval_ground_truth.yaml")
KS = (3, 5, 8, 10, 14, 20, 40)


def load_cases(known):
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    out = []
    for e in gt:
        if e.get("expect") == "schema_unsupported" or not e.get("sql"):
            continue
        try:
            need, _ = required_schema(e["sql"], known)
        except Exception:
            continue
        if need:
            out.append((e["id"], e["question"], need))
    return out


# ===========================================================================
# A：列數當熱度的代理
# ===========================================================================

def row_counts() -> dict[str, int]:
    """真實列數。INFORMATION_SCHEMA.TABLE_ROWS 對 InnoDB 是估計值，
    會有 ±10% 的誤差；這裡要拿它做排序，所以老實 COUNT(*)。"""
    db = get_db_manager(MYSQL_URI)
    with db.engine.connect() as conn:
        names = [r[0].lower() for r in conn.execute(text(
            "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE()"))]
        return {t: conn.execute(text(f"SELECT COUNT(*) FROM {t}")).scalar()
                for t in names}


def popularity(counts: dict[str, int]) -> dict[str, float]:
    """log 壓縮後正規化到 [0,1]。用 log 是因為列數跨三個數量級，
    線性的話 browse_logs(665) 會把所有小表壓成 0。"""
    import math
    mx = math.log1p(max(counts.values()) or 1)
    return {t: math.log1p(n) / mx for t, n in counts.items()}


# ===========================================================================
# C：值／實體檢索（CHESS 的簡化版）
# ===========================================================================

def value_index(max_card: int = 60) -> dict[str, set[str]]:
    """{欄位值: {擁有這個值的表}}。只收低基數的文字欄位 ——
    高基數的（email、tracking_no）不可能出現在自然語言問題裡。

    中文問題直接用子字串比對就夠，不必像 CHESS 那樣上 LSH ——
    LSH 是為了容忍英文拼字錯誤，這裡的值是中文類別名與狀態碼。
    """
    db = get_db_manager(MYSQL_URI)
    idx: dict[str, set[str]] = defaultdict(set)
    with db.engine.connect() as conn:
        cols = conn.execute(text("""
            SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME)
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = DATABASE()
              AND DATA_TYPE IN ('varchar', 'char', 'enum')
        """)).fetchall()
        for t, c in cols:
            n = conn.execute(text(
                f"SELECT COUNT(DISTINCT `{c}`) FROM `{t}`")).scalar() or 0
            if not 0 < n <= max_card:
                continue
            for (v,) in conn.execute(text(
                    f"SELECT DISTINCT `{c}` FROM `{t}` WHERE `{c}` IS NOT NULL")):
                v = str(v).strip()
                if len(v) >= 2:
                    idx[v].add(t)
    return dict(idx)


def value_hits(question: str, idx: dict[str, set[str]]) -> dict[str, int]:
    """這一題的文字裡命中了哪些表的實際值，各幾次。"""
    q = question.lower()
    hit: dict[str, int] = defaultdict(int)
    for v, tables in idx.items():
        if v.lower() in q:
            for t in tables:
                hit[t] += 1
    return dict(hit)


# ===========================================================================
# D：欄位優先並聯
# ===========================================================================

def column_owner_scores(qvec, colvecs, top_m: int) -> dict[str, float]:
    """取相似度最高的 top_m 個欄位，回傳 {擁有它的表: 最高的那個欄位分數}。"""
    ranked = sorted(((name, _cosine(qvec, v)) for name, v in colvecs.items()),
                    key=lambda p: -p[1])[:top_m]
    best: dict[str, float] = {}
    for name, sc in ranked:
        t = name.split(".", 1)[0]
        best[t] = max(best.get(t, 0.0), sc)
    return best


# ===========================================================================

def evaluate(name: str, rankings: list[list[str]], cases, distractors) -> dict:
    rec = {}
    for k in KS:
        ok = sum(1 for (_, _, need), r in zip(cases, rankings) if need <= set(r[:k]))
        rec[k] = ok / len(cases) * 100
    top1 = sum(1 for (_, _, need), r in zip(cases, rankings) if r[0] in need)
    dist1 = sum(1 for r in rankings if r[0] in distractors)
    return {"name": name, "recall": rec,
            "top1": top1 / len(cases) * 100, "dist1": dist1}


def report(rows: list[dict]) -> None:
    head = f"{'變體':<34}" + "".join(f"@{k:<5}" for k in KS) + f"{'Top-1':>8}{'干擾搶1':>9}"
    print(head)
    print("-" * len(head))
    for r in rows:
        line = f"{r['name']:<34}" + "".join(f"{r['recall'][k]:5.1f} " for k in KS)
        print(line + f"{r['top1']:7.1f}%{r['dist1']:8d}")


def main() -> int:
    only = None
    if "--only" in sys.argv:
        only = set(sys.argv[sys.argv.index("--only") + 1:])

    known = get_table_columns()
    cases = load_cases(known)
    yaml_tables = {k.lower() for k in get_schema_parser().get_table_columns()}
    distractors = set(known) - yaml_tables
    print(f"題數 {len(cases)}；全庫 {len(known)} 張表（干擾表 {len(distractors)} 張）\n")

    tvecs = get_table_vectors()
    qvecs = _embed([q for _, q, _ in cases], "query")
    base_scores = [{t: _cosine(qv, v) for t, v in tvecs.items()} for qv in qvecs]

    def rank(scores_list):
        return [[t for t, _ in sorted(s.items(), key=lambda p: (-p[1], p[0]))]
                for s in scores_list]

    rows = [evaluate("baseline（純表向量）", rank(base_scores), cases, distractors)]

    if not only or "A" in only:
        pop = popularity(row_counts())
        for alpha in (0.02, 0.05, 0.10):
            mod = [{t: s + alpha * pop.get(t, 0.0) for t, s in sc.items()}
                   for sc in base_scores]
            rows.append(evaluate(f"A 列數先驗 α={alpha}", rank(mod), cases, distractors))

    if not only or "C" in only:
        idx = value_index()
        print(f"值索引：{len(idx)} 個相異值\n")
        for beta in (0.02, 0.05, 0.10):
            mod = []
            for sc, (_id, q, _n) in zip(base_scores, cases):
                h = value_hits(q, idx)
                mod.append({t: s + beta * min(h.get(t, 0), 3) for t, s in sc.items()})
            rows.append(evaluate(f"C 值檢索 β={beta}", rank(mod), cases, distractors))

    if not only or "D" in only:
        keys, comments, cols_of = load_meta()
        colvecs = column_vectors(comments, cols_of)
        for top_m, gamma in ((10, 0.5), (20, 0.5), (20, 1.0)):
            mod = []
            for sc, qv in zip(base_scores, qvecs):
                own = column_owner_scores(qv, colvecs, top_m)
                mod.append({t: max(s, gamma * own[t]) if t in own else s
                            for t, s in sc.items()})
            rows.append(evaluate(f"D 欄位優先 M={top_m} γ={gamma}",
                                 rank(mod), cases, distractors))

    print()
    report(rows)
    print("\n註：α/β/γ 是刻意掃的曲線，不是選出來的參數。看的是**有沒有方向**，")
    print("    不是哪個數字最高 —— 單點最高值在 155 題上分不出 1pp 的差異（§5.2）。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
