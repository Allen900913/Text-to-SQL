# -*- coding: utf-8 -*-
"""NIM 嵌入的最底層：呼叫、雜湊、餘弦。**不 import 任何其他 utils。**

為什麼要抽出來（2026-08-26）：`column_hints` 需要嵌入，而它被 `table_filter` 用，
`table_filter` 又被 `table_retriever` 用 —— 嵌入若留在 `table_retriever` 就是循環。
這個模組刻意只依賴 `config`，是依賴圖的葉子。

`table_retriever` 仍然以 `_embed` / `_doc_hash` / `_cosine` 的舊名字再匯出，
所以既有的 eval 腳本（它們直接 `from ...table_retriever import _embed, _cosine`）
一行都不用改。
"""
import hashlib
import math
import threading

import requests

from langgraph_sql.config import NVIDIA_API_KEY

EMBED_URL = "https://integrate.api.nvidia.com/v1/embeddings"
EMBED_MODEL = "nvidia/nemotron-3-embed-1b"

# 問句向量的記憶體快取。同一個問句在一次 pipeline 裡會被嵌入兩次
# （table_retriever 排表一次、column_hints 挑欄位一次），
# 沒有這層就是每題多一次 API 呼叫 —— 309 題就是多 309 次。
_QCACHE: dict[str, list[float]] = {}
_QCACHE_MAX = 512
_qlock = threading.Lock()


def doc_hash(doc: str) -> str:
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]


def embed(texts: list[str], kind: str) -> list[list[float]]:
    """呼叫 NIM embedding。kind 是 'passage'（文件）或 'query'（問題）。"""
    headers = {"Authorization": f"Bearer {NVIDIA_API_KEY}",
               "Accept": "application/json"}
    out: list[list[float]] = []
    for i in range(0, len(texts), 32):
        resp = requests.post(EMBED_URL, headers=headers, timeout=60, json={
            "input": texts[i:i + 32], "model": EMBED_MODEL, "input_type": kind,
            "encoding_format": "float", "truncate": "END",
        })
        resp.raise_for_status()
        data = sorted(resp.json()["data"], key=lambda d: d["index"])
        out += [d["embedding"] for d in data]
    return out


def embed_query(query: str) -> list[float]:
    """問句向量，帶記憶體快取。**同一個問句只會真的嵌入一次。**"""
    hit = _QCACHE.get(query)
    if hit is not None:
        return hit
    vec = embed([query], "query")[0]
    with _qlock:
        if len(_QCACHE) >= _QCACHE_MAX:
            _QCACHE.clear()          # 粗暴但夠用：這是單次評估跑的暫存，不是長駐服務
        _QCACHE[query] = vec
    return vec


def cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
