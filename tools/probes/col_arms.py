# -*- coding: utf-8 -*-
"""欄位級標註三臂探針：在候選目錄裡替每張表列欄位，選表會不會變準？

**與路線 ②（§2.7d/e）的差別**，每一點都是那一輪的教訓：

  1. D 臂挑的是「概念群」再取該群宣告時的**前三個欄位**（`cols[:3]`）——
     跟問句無關。這裡直接對 869 個欄位算餘弦，少一層轉換。
  2. 實測正解欄位在全庫 869 欄裡排 **1~2 名**（八題裡七題），
     而且都是自己表內的**第 1 名** —— 所以 k=2 就夠，不用列一長串。
  3. `#284` 的前 8 名全是 shipment_profiles / shipments，
     那張把模型騙走的 `product_tags` **一個都沒進來**。

**這一層加容量是安全的，加在檢索層不安全**（§2.7b 寬表誤選 6 → 21）：
檢索層是 N 份文件取 max，拆越多份中 hub 的機會越多；
候選目錄裡每張表只列一次，不存在取最大值，所以目錄只會變長，不會變偏。

三臂：
  A  現行 —— 表名 + 表註解
  F  固定列 —— 每表列 k 個「最有辨識度」的欄位，**與問句無關**（不做主張）
  E  檢索挑 —— 每表列 k 個與問句最像的欄位（做主張）

F 存在的理由：D 的教訓是「不做主張幾乎沒有下行風險但收益小，做主張雙向」。
要分辨收益來自「多了欄位資訊」還是「挑對了欄位」，就得有一個不做主張的對照。

三臂共用同一組候選與同一個 shuffle seed（production 用問句的 CRC32），
差異只來自目錄文字本身。
"""
import json
import os
import sys
import zlib
from collections import Counter

_ROOT = r"C:\Text-to-SQL"
_OLD = (r"C:\Users\uscc\AppData\Local\Temp\claude\c--Text-to-SQL"
        r"\62994ace-3c0c-4676-ad64-04cc195b2762\scratchpad")
for p in (_ROOT, os.path.join(_ROOT, "eval"), _OLD,
          os.path.dirname(os.path.abspath(__file__))):
    sys.path.insert(0, p)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from col_probe import column_docs  # noqa: E402
from eval_retrieval import load_cases  # noqa: E402
from route2_probe import catalog, pick  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.table_filter import get_candidate_n, get_table_briefs  # noqa: E402
from langgraph_sql.utils.table_retriever import _cosine, _embed, get_table_vectors  # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "col_vecs.json")
VOTES = 8
K = 2                      # 每表列幾欄：實測正解欄位是自己表內第 1 名（8 題裡 7 題）

# 四題誘餌（選表層穩定錯）+ 三題對照（現在會過，看有沒有反效果）
BAIT = [282, 284, 292, 308]
CONTROL = [261, 263, 280]


def col_comments():
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        db = c.execute(text("SELECT DATABASE()")).scalar()
        return {(t, col): (cm or "").strip() for t, col, cm in c.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=:d"), {"d": db})}


def distinctive(cvecs, by_table):
    """每張表最「不像大家」的欄位 —— 與全庫欄位重心的餘弦最低者。

    為什麼用重心：`created_at`、`is_active` 這種到處都有的欄位會很靠近重心，
    列出來等於沒列。真正能區分一張表的是離重心遠的那幾個。
    純粹是幾何性質，**與問句無關**，所以這一臂不做任何相關性主張。
    """
    dim = len(next(iter(cvecs.values())))
    cen = [0.0] * dim
    for v in cvecs.values():
        for i, x in enumerate(v):
            cen[i] += x
    cen = [x / len(cvecs) for x in cen]
    return {t: sorted(ks, key=lambda k: _cosine(cvecs[k], cen))[:K]
            for t, ks in by_table.items()}


def line(table, cols, cmts, head):
    if not cols:
        return ""
    body = "、".join(f"{c}（{cmts.get((t, c), '')}）" for t, c in cols)
    return f"\n    ▸ {head}：{body}"


def main():
    log.remove()
    docs = column_docs()
    raw = json.load(open(CACHE, encoding="utf-8"))
    cvecs = {tuple(k.split("\t")): v for k, v in raw.items()}
    assert len(cvecs) == len(docs), f"快取 {len(cvecs)} 份 ≠ 文件 {len(docs)} 份，先重跑 col_probe.py"
    cmts = col_comments()

    by_table = {}
    for t, c in cvecs:
        by_table.setdefault(t, []).append((t, c))
    fixed = distinctive(cvecs, by_table)

    cases = {qid: (q, needs) for qid, q, needs in load_cases()}
    briefs = get_table_briefs()
    tvecs = get_table_vectors()
    ids = [i for i in BAIT + CONTROL if i in cases]
    qvecs = dict(zip(ids, _embed([cases[i][0] for i in ids], "query")))

    print(f"欄位 {len(cvecs)} 份｜每表列 K={K} 欄｜候選 {get_candidate_n(len(briefs))} 張")
    print(f"{len(ids)} 題 × 3 臂 × {VOTES} 票 = {len(ids) * 3 * VOTES} 次選表呼叫\n")
    print(f"{'題':>5s}  {'需要的表':30s}  A現行  F固定欄  E檢索欄   缺表(E)")

    tot = {"A": 0, "F": 0, "E": 0}
    rows = []
    for qid in ids:
        q, needs = cases[qid]
        qv = qvecs[qid]
        cands = sorted(tvecs, key=lambda t: -_cosine(qv, tvecs[t]))[
            :get_candidate_n(len(briefs))]
        seed = zlib.crc32(q.encode("utf-8"))
        picked = {t: sorted(by_table.get(t, []), key=lambda k: -_cosine(qv, cvecs[k]))[:K]
                  for t in cands}
        cats = {
            "A": catalog(cands, briefs, {}, seed),
            "F": catalog(cands, briefs,
                         {t: line(t, fixed.get(t, []), cmts, "代表欄位") for t in cands}, seed),
            "E": catalog(cands, briefs,
                         {t: line(t, picked[t], cmts, "本題可能相關的欄位") for t in cands}, seed),
        }
        hit, miss = {}, Counter()
        for name, cat in cats.items():
            h = 0
            for _ in range(VOTES):
                got = set(pick(q, cat))
                if any(nd <= got for nd in needs):
                    h += 1
                elif name == "E":
                    for t in (min(needs, key=len) - got):
                        miss[t] += 1
            hit[name] = h
            tot[name] += h
        need_s = "+".join(sorted(min(needs, key=len)))
        tag = "誘餌" if qid in BAIT else "對照"
        rows.append((qid, tag, hit))
        print(f"#{qid:<4d}  {need_s:30s}  "
              + "   ".join(f"{hit[k]:>2d}/{VOTES}" for k in ("A", "F", "E"))
              + f"    {dict(miss) if miss else ''}", flush=True)

    print("\n" + "=" * 80)
    for tag in ("誘餌", "對照"):
        sub = [r for r in rows if r[1] == tag]
        if not sub:
            continue
        n = len(sub) * VOTES
        print(f"{tag} {len(sub):2d} 題   " + "   ".join(
            f"{k} {sum(r[2][k] for r in sub):3d}/{n}" for k in ("A", "F", "E")))
    n = len(rows) * VOTES
    print(f"合計 {len(rows):2d} 題   " + "   ".join(
        f"{k} {tot[k]:3d}/{n}" for k in ("A", "F", "E")))
    for arm in ("F", "E"):
        w = [r[0] for r in rows if r[2][arm] < r[2]["A"]]
        b = [r[0] for r in rows if r[2][arm] > r[2]["A"]]
        print(f"{arm} vs A：變好 {b}、退步 {w}")
    print(f"目錄長度（最後一題）：A {len(cats['A']):,}、"
          f"F {len(cats['F']):,}、E {len(cats['E']):,} 字元")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
