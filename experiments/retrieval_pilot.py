"""
檢索先導實驗 — 表級檢索該用什麼？
====================================
在擴充 schema 之前，先用 21 張合成表回答三個設計問題：

  1. 表級檢索需要 dense + sparse 混合嗎，還是純語意就夠？
  2. 欄位級檢索呢？
  3. 表和欄位都是「名稱 + 說明 + 範例」，需要兩套索引嗎？

這個實驗全程沒有用到任何一列資料 —— 檢索評估只需要 schema 寬度，
不需要資料量。這件事本身就改變了工作順序：擴表（便宜）與灌資料（貴）
可以拆開，前者就足以解鎖所有檢索決策。

量測指標刻意用「錨點 Recall」與「經 FK 路徑補完後的 Recall」兩層，
因為橋接表在語意上是隱形的 —— 問「買了哪些手機」時，order_items
不會被任何字詞命中，那不是檢索該負責的事。

⚠️ 樣本只有 6 個問題 / 21 張表，每題佔 17%，誤差條非常大。
   這組數字的作用是排除選項與定出量級，不是定案。

執行：python experiments/retrieval_pilot.py
"""
import heapq
import math
import os
import re
import sys
from collections import Counter

import requests
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# 合成 schema：刻意包含 browse_logs / carts 這類「行為表」，
# 它們會在 customers 與 products 之間製造語意錯誤的捷徑。
# ---------------------------------------------------------------------------
TABLES = {
    "customers": "客戶資訊表，記錄每一位客戶的姓名、信箱、電話、居住城市與註冊時間",
    "orders": "訂單主表，記錄每一張訂單的下單客戶、下單日期、總金額與訂單狀態",
    "order_items": "訂單明細表，記錄每一張訂單裡買了哪些商品、各買幾件、成交單價",
    "products": "商品資訊表，記錄商品名稱、所屬分類、定價、庫存數量與商品描述",
    "categories": "商品分類表，記錄分類名稱與階層關係",
    "suppliers": "供應商表，記錄供貨廠商的名稱、聯絡方式與合作起始日",
    "product_specs": "商品規格表，記錄商品的尺寸、重量、材質、顏色等細部規格",
    "carts": "購物車表，記錄客戶尚未結帳的購物車",
    "cart_items": "購物車明細，記錄購物車裡放了哪些商品",
    "reviews": "商品評價表，記錄客戶對商品的評分與評論文字",
    "payments": "付款紀錄表，記錄每一筆款項的金額、付款時間與付款結果",
    "payment_methods": "付款方式表，記錄可用的付款管道，例如信用卡、貨到付款、轉帳",
    "shipments": "出貨紀錄表，記錄每張訂單的出貨時間、物流單號與收件地址",
    "warehouses": "倉庫表，記錄各倉庫的名稱與所在地區",
    "refunds": "退款紀錄表，記錄退款金額、退款原因與處理狀態",
    "invoices": "發票表，記錄開立發票的號碼、金額與開立日期",
    "addresses": "地址簿，記錄客戶儲存的收件地址",
    "browse_logs": "瀏覽紀錄表，記錄客戶在網站上瀏覽過哪些商品、停留多久",
    "promotions": "促銷活動表，記錄檔期名稱、折扣幅度與活動起訖時間",
    "order_promotions": "訂單套用的促銷，記錄某張訂單使用了哪些活動",
    "user_profiles": "會員檔案，記錄會員等級、累積點數、偏好設定與生日",
}

# (問題, 回答這題必須看得到的表)
TESTS = [
    ("陳先生上個月買了哪些蘋果手機？", {"customers", "orders", "order_items", "products"}),
    ("哪些客戶最常退貨？", {"refunds", "payments", "orders", "customers"}),
    ("用貨到付款的訂單有幾張？", {"payment_methods", "payments", "orders"}),
    ("哪個倉庫出貨最慢？", {"shipments", "warehouses"}),
    ("會員等級跟消費金額有關係嗎？", {"user_profiles", "customers", "orders"}),
    ("看過但沒買的商品有哪些？", {"browse_logs", "order_items", "products"}),
]

EDGES = [
    ("orders", "customers"), ("order_items", "orders"), ("order_items", "products"),
    ("products", "categories"), ("products", "suppliers"), ("product_specs", "products"),
    ("carts", "customers"), ("cart_items", "carts"), ("cart_items", "products"),
    ("reviews", "customers"), ("reviews", "products"), ("payments", "orders"),
    ("payments", "payment_methods"), ("shipments", "orders"), ("shipments", "warehouses"),
    ("refunds", "payments"), ("invoices", "orders"), ("addresses", "customers"),
    ("browse_logs", "customers"), ("browse_logs", "products"), ("promotions", "categories"),
    ("order_promotions", "orders"), ("order_promotions", "promotions"),
    ("user_profiles", "customers"),
]

# 行為／日誌類的表：邊加權，避免 customers → browse_logs → products 這種
# 拓撲最短、語意錯誤的捷徑（問「買了什麼」卻走瀏覽紀錄）。
COSTLY_TABLES = {"browse_logs", "carts", "cart_items", "reviews"}

EMBED_URL = "https://integrate.api.nvidia.com/v1/embeddings"
MODELS = [
    "nvidia/nemotron-3-embed-1b",
    "nvidia/nv-embedqa-e5-v5",
]

NAMES = list(TABLES)
DOCS = [f"{k}：{v}" for k, v in TABLES.items()]


# ===========================================================================
# FK 路徑（Steiner Tree 的最短路徑啟發式，帶邊權重）
# ===========================================================================

def build_graph() -> dict[str, dict[str, int]]:
    g: dict[str, dict[str, int]] = {}
    for a, b in EDGES:
        w = 4 if (a in COSTLY_TABLES or b in COSTLY_TABLES) else 1
        g.setdefault(a, {})[b] = w
        g.setdefault(b, {})[a] = w
    return g


def dijkstra(g, src):
    dist, prev, pq = {src: 0}, {}, [(0, src)]
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist.get(u, math.inf):
            continue
        for v, w in sorted(g.get(u, {}).items()):
            if d + w < dist.get(v, math.inf):
                dist[v], prev[v] = d + w, u
                heapq.heappush(pq, (d + w, v))
    return dist, prev


def steiner(g, anchors: list[str]) -> set[str]:
    known = [a for a in anchors if a in g]
    if len(known) <= 1:
        return set(anchors)
    tree, remaining = {known[0]}, set(known[1:])
    while remaining:
        best = None
        for anchor in sorted(remaining):
            dist, prev = dijkstra(g, anchor)
            for target in sorted(tree):
                if target not in dist:
                    continue
                path = [target]
                while path[-1] != anchor:
                    path.append(prev[path[-1]])
                if best is None or (dist[target], path) < best:
                    best = (dist[target], path)
        if best is None:
            tree |= remaining
            break
        tree |= set(best[1])
        remaining -= tree
    return tree | {a for a in anchors if a not in g}


# ===========================================================================
# 檢索：BM25（字元 bigram）與 dense
# ===========================================================================

def tokenize(text: str) -> list[str]:
    """中文用字元 bigram，英文識別字整段當一個 token。"""
    clean = re.sub(r"[，。、？：\s（）]", "", text)
    return [clean[i:i + 2] for i in range(len(clean) - 1)] + re.findall(r"[a-z_]+", text.lower())


class BM25:
    def __init__(self, docs: list[str], k1: float = 2.5, b: float = 0.75):
        self.docs = [tokenize(d) for d in docs]
        self.k1, self.b = k1, b
        self.n = len(self.docs)
        self.avg = sum(map(len, self.docs)) / self.n
        self.df = Counter(g for d in self.docs for g in set(d))

    def rank(self, query: str) -> list[int]:
        terms = set(tokenize(query))
        scores = []
        for doc in self.docs:
            tf, score = Counter(doc), 0.0
            for t in terms:
                if t not in tf:
                    continue
                idf = math.log((self.n - self.df[t] + 0.5) / (self.df[t] + 0.5) + 1)
                score += idf * tf[t] * (self.k1 + 1) / (
                    tf[t] + self.k1 * (1 - self.b + self.b * len(doc) / self.avg))
            scores.append(score)
        return sorted(range(self.n), key=lambda i: -scores[i])


def embed(texts: list[str], kind: str, model: str, headers: dict) -> list[list[float]]:
    out = []
    for i in range(0, len(texts), 32):
        resp = requests.post(EMBED_URL, headers=headers, timeout=120, json={
            "input": texts[i:i + 32], "model": model, "input_type": kind,
            "encoding_format": "float", "truncate": "END",
        })
        resp.raise_for_status()
        out += [d["embedding"] for d in sorted(resp.json()["data"], key=lambda d: d["index"])]
    return out


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def rrf(rankings: list[list[int]], k: int = 60) -> list[int]:
    score: Counter = Counter()
    for ranking in rankings:
        for pos, idx in enumerate(ranking):
            score[idx] += 1 / (k + pos + 1)
    return [i for i, _ in score.most_common()]


# ===========================================================================

def main() -> int:
    load_dotenv(".env")
    key = os.getenv("NVIDIA_API_KEY")
    if not key:
        print("需要 NVIDIA_API_KEY")
        return 1
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}
    graph = build_graph()

    bm25 = BM25(DOCS)
    rankers: dict[str, callable] = {"BM25": bm25.rank}

    for model in MODELS:
        try:
            doc_vecs = embed(DOCS, "passage", model, headers)
            q_vecs = dict(zip([q for q, _ in TESTS],
                              embed([q for q, _ in TESTS], "query", model, headers)))
        except Exception as exc:
            print(f"{model} 不可用: {str(exc)[:70]}")
            continue
        rankers[model.split("/")[-1]] = (
            lambda q, dv=doc_vecs, qv=q_vecs: sorted(
                range(len(NAMES)), key=lambda i: -cosine(qv[q], dv[i]))
        )

    if "nemotron-3-embed-1b" in rankers:
        dense = rankers["nemotron-3-embed-1b"]
        rankers["RRF(BM25+nemotron)"] = lambda q: rrf([bm25.rank(q), dense(q)])

    print(f"{'方法':<24}{'K':>3}{'錨點Recall':>12}{'+FK路徑後':>12}{'平均表數':>10}"
          f"{'佔' + str(len(NAMES)) + '張':>9}")
    print("-" * 72)
    for label, rank_fn in rankers.items():
        for k in (2, 3, 4, 5):
            anchor_hit = final_hit = total = size = 0
            for question, need in TESTS:
                anchors = [NAMES[i] for i in rank_fn(question)[:k]]
                selected = steiner(graph, anchors)
                anchor_hit += len(need & set(anchors))
                final_hit += len(need & selected)
                total += len(need)
                size += len(selected)
            avg = size / len(TESTS)
            print(f"{label:<24}{k:>3}{anchor_hit / total * 100:>11.0f}%"
                  f"{final_hit / total * 100:>11.0f}%{avg:>10.1f}"
                  f"{avg / len(NAMES) * 100:>8.0f}%")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
