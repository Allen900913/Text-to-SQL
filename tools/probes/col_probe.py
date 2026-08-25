# -*- coding: utf-8 -*-
"""欄位級檢索能不能救選表層？（零 LLM，純嵌入＋餘弦）

背景：選表那一步 LLM **只看得到表名＋表註解**（`get_table_briefs` 的 docstring
自己寫著「不含欄位」）。而三題穩定錯的正解欄位，註解跟問句幾乎逐字對應：

  #282 中途改過地址  → shipment_profiles.is_redirected 「是否中途改過送件地址」
  #292 跟對帳單勾稽  → payment_profiles.is_reconciled  「是否已與金流對帳單勾稽」
  #308 超過可退期限  → return_profiles.is_over_policy_window「申請時是否已超過可退貨期限」

**這與 §2.7b 被否決的「概念群文件」不是同一層。** 那一次是把表拆成 N 份
*檢索文件*，於是寬表有 N 次機會被取 max，寬表誤選 6 → 19。這裡不動檢索層，
只問：如果在候選目錄裡替每張表列出「與這一題最像的幾個欄位」，
正解欄位排得進去嗎？目錄裡每張表只出現一次，不存在取最大值。

這支只量「排名」，不呼叫 LLM，也不改任何東西。排不進去就沒有下一步可談。
"""
import json
import os
import sys

_ROOT = r"C:\Text-to-SQL"
for p in (_ROOT, os.path.join(_ROOT, "eval")):
    sys.path.insert(0, p)

from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.table_filter import get_candidate_n, get_table_briefs  # noqa: E402
from langgraph_sql.utils.table_retriever import _cosine, _embed, get_table_vectors  # noqa: E402

CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "col_vecs.json")

# 三題穩定錯 + 兩題生成層硬幣 + 三題對照（現在會過的，看有沒有反效果）
CASES = {
    282: ("有沒有哪些出貨是中途改過地址的？改的原因是什麼？", "shipment_profiles"),
    284: ("有哪幾筆出貨裡面含有標成易碎品的商品？這些出貨的外箱有沒有貼易碎標籤、"
          "用什麼緩衝材、封箱方式是什麼？", "product_profiles"),
    292: ("有哪些付款到現在還沒跟對帳單勾稽完成？", "payment_profiles"),
    308: ("有沒有客人是超過可退期限才申請退貨的，最後還是讓他退了？", "return_profiles"),
    287: ("有沒有評價，在評價內容檔案上登記的「到貨後天數」超過三十天？", "review_profiles"),
    263: ("哪些訂單的風控覆核沒過？是誰覆核的、什麼時候覆核的、有沒有客訴？",
          "order_profiles"),
    261: ("內容完整度低於 60% 的商品，內容檔案上登記的圖片張數、是否有影片、"
          "是否附說明書與翻譯狀態各是什麼？", "product_profiles"),
    280: ("列出所有派送過程發生過異常的出貨，其異常代碼、異常說明、上門次數、"
          "最後掃描地點與掃描時間。", "shipment_profiles"),
}

# 每題「正解欄位」——用來看排名。多個就取最好的那個。
GOLD = {
    282: [("shipment_profiles", "is_redirected"), ("shipment_profiles", "redirect_reason")],
    284: [("product_profiles", "is_fragile"), ("shipment_profiles", "has_fragile_label")],
    292: [("payment_profiles", "is_reconciled")],
    308: [("return_profiles", "is_over_policy_window")],
    287: [("review_profiles", "days_after_delivery")],
    263: [("order_profiles", "fraud_review_result")],
    261: [("product_profiles", "content_completeness"), ("product_profiles", "image_count")],
    280: [("shipment_profiles", "exception_code"), ("shipment_profiles", "attempt_count")],
}


def column_docs():
    """每個欄位一份短文件：`表註解的前段 · 欄位名（欄位註解）`。

    為什麼要帶表註解的前段：`is_active`、`created_at` 這種欄位光看註解
    在 93 張表裡是重複的，不帶表的脈絡就分不出是誰的。只取前 18 字，
    避免整段表註解把欄位註解稀釋掉（§2.7 稀釋定律形式 ①）。
    """
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        db = c.execute(text("SELECT DATABASE()")).scalar()
        rows = c.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=:d ORDER BY TABLE_NAME, ORDINAL_POSITION"), {"d": db}).fetchall()
    briefs = get_table_briefs()
    docs = {}
    for t, col, cm in rows:
        if col in ("id",) or col.endswith("_id"):
            continue                      # 主鍵外鍵不帶語意，只會製造雜訊
        head = (briefs.get(t.lower(), "") or "").split("：")[0][:18]
        docs[(t, col)] = f"{head} · {col}（{(cm or '').strip()}）"
    return docs


def cached(docs):
    if os.path.exists(CACHE):
        raw = json.load(open(CACHE, encoding="utf-8"))
        if len(raw) == len(docs):
            return {tuple(k.split("\t")): v for k, v in raw.items()}
    keys = sorted(docs)
    vecs = {}
    B = 96
    for i in range(0, len(keys), B):
        chunk = keys[i:i + B]
        for k, v in zip(chunk, _embed([docs[k] for k in chunk], "passage")):
            vecs[k] = v
        print(f"    嵌入 {min(i + B, len(keys))}/{len(keys)}", flush=True)
    json.dump({"\t".join(k): v for k, v in vecs.items()},
              open(CACHE, "w", encoding="utf-8"))
    return vecs


def main():
    log.remove()
    docs = column_docs()
    print(f"欄位文件 {len(docs)} 份（已排除 id/*_id）")
    cvecs = cached(docs)
    tvecs = get_table_vectors()
    n_cand = get_candidate_n(len(tvecs))
    qs = {qid: q for qid, (q, _t) in CASES.items()}
    qvecs = dict(zip(qs, _embed(list(qs.values()), "query")))

    print(f"\n候選 {n_cand} 張 / {len(tvecs)} 張表\n")
    print(f"{'題':>5s}  {'需要的表':20s} {'表排名':>7s}  {'正解欄位':34s} {'欄位排名':>9s}  同表內")
    print("-" * 104)
    for qid, (q, want) in CASES.items():
        qv = qvecs[qid]
        trank = sorted(tvecs, key=lambda t: -_cosine(qv, tvecs[t])).index(want) + 1
        order = sorted(cvecs, key=lambda k: -_cosine(qv, cvecs[k]))
        best, brank = None, None
        for g in GOLD[qid]:
            if g in cvecs:
                r = order.index(g) + 1
                if brank is None or r < brank:
                    best, brank = g, r
        # 同表內排第幾（決定「每表列前 k 欄」的 k 要多大）
        same = [k for k in order if k[0] == best[0]]
        inrank = same.index(best) + 1
        print(f"#{qid:<4d}  {want:20s} {trank:>4d}/93  {best[0] + '.' + best[1]:34s} "
              f"{brank:>5d}/{len(cvecs)}  第 {inrank} 欄")

    print("\n每題最像的前 8 個欄位（不分表）——這就是目錄裡會多出來的東西：")
    for qid, (q, want) in CASES.items():
        qv = qvecs[qid]
        top = sorted(cvecs, key=lambda k: -_cosine(qv, cvecs[k]))[:8]
        hit = {g for g in GOLD[qid]}
        s = "、".join(("★" if k in hit else "") + f"{k[0]}.{k[1]}" for k in top)
        print(f"  #{qid}: {s}")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
