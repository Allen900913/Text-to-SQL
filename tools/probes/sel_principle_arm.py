# 選表層加第 5 條原則有沒有用？12 題 × 2 臂 × 8 票。
# 原則寫成「哪一類屬性住在哪一類表」，不是「哪一題要用哪張表」——
# 後者是 teaching to the test，而且換個 schema 就失效（memory: few-shot-not-rules）。
import sys, os, zlib
sys.stdout.reconfigure(encoding='utf-8')
sys.path.insert(0, r"C:\Text-to-SQL"); sys.path.insert(0, r"C:\Text-to-SQL\eval")
sys.path.insert(0, r"C:\Users\uscc\AppData\Local\Temp\claude\c--Text-to-SQL"
                   r"\62994ace-3c0c-4676-ad64-04cc195b2762\scratchpad")
from loguru import logger as log
from eval_retrieval import load_cases
import route2_probe as rp
from langgraph_sql.utils import table_filter as tf
from langgraph_sql.utils.table_filter import format_catalog, get_candidate_n, get_table_briefs
from langgraph_sql.utils.table_retriever import _cosine, _embed_query, get_table_vectors
log.remove()
VOTES = 8
EXTRA = """
5. 同一個概念可能同時存在於「明細表」與「檔案寬表」兩個地方。明細表記的是
   這筆紀錄本身（誰、什麼、多少、什麼時候），檔案寬表記的是它的**狀態旗標、
   作業細節與設定值**。問題問的是後者時，明細表裡通常沒有那個欄位。"""
BAIT = [282, 284, 292, 308]
WIDE = [261, 263, 280]
NARROW = [11, 24, 25, 39, 132]
cases = {q: (t, n) for q, t, n in load_cases()}
briefs, tvecs = get_table_briefs(), get_table_vectors()
ids = [i for i in BAIT + WIDE + NARROW if i in cases]
print(f"{len(ids)} 題 × 2 臂 × {VOTES} 票 = {len(ids)*2*VOTES} 次呼叫\n", flush=True)
print(f"{'題':>5s} {'類別':6s} {'需要的表':38s}  A現行   G加第5條", flush=True)
BASE = rp._SYSTEM_PROMPT
tot = {"A": 0, "G": 0}; rows = []
for qid in ids:
    q, needs = cases[qid]
    qv = _embed_query(q)
    cands = sorted(tvecs, key=lambda t: -_cosine(qv, tvecs[t]))[:get_candidate_n(len(briefs))]
    cat = format_catalog(cands, zlib.crc32(q.encode("utf-8")))
    hit = {}
    for name, sysp in (("A", BASE), ("G", BASE.replace(
            "\n\n輸出格式：", EXTRA + "\n\n輸出格式："))):
        rp._SYSTEM_PROMPT = sysp
        hit[name] = sum(1 for _ in range(VOTES)
                        if any(nd <= set(rp.pick(q, cat)) for nd in needs))
        tot[name] += hit[name]
    rp._SYSTEM_PROMPT = BASE
    tag = "誘餌" if qid in BAIT else ("寬表對照" if qid in WIDE else "窄表對照")
    rows.append((qid, tag, hit))
    print(f"#{qid:<4d} {tag:6s} {'+'.join(sorted(min(needs,key=len)))[:38]:38s}  "
          f"{hit['A']:>2d}/{VOTES}   {hit['G']:>2d}/{VOTES}", flush=True)
print("\n" + "="*72)
for tag in ("誘餌", "寬表對照", "窄表對照"):
    sub = [r for r in rows if r[1] == tag]
    n = len(sub)*VOTES
    print(f"{tag:8s} {len(sub):2d} 題   A {sum(r[2]['A'] for r in sub):3d}/{n}   "
          f"G {sum(r[2]['G'] for r in sub):3d}/{n}")
n = len(ids)*VOTES
print(f"{'合計':8s} {len(ids):2d} 題   A {tot['A']:3d}/{n}   G {tot['G']:3d}/{n}")
print(f"G vs A：變好 {[r[0] for r in rows if r[2]['G']>r[2]['A']]}、"
      f"退步 {[r[0] for r in rows if r[2]['G']<r[2]['A']]}")
