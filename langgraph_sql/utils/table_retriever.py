"""
Table Retriever — 從問題找出該進 Prompt 的表
====================================================================
為什麼需要它：

21 張表全塞進 Prompt 是 18,459 字元，其中 DDL 佔 50.3%。但實測 145 題
平均每題只需要 1.8 張表（62 題只需 1 張、54 題需 2 張）。也就是說模型
每次都在讀十倍於它需要的 schema，而且表愈多這個比例愈糟。

這支模組是三段漏斗的第一段與總調度：

    相似度（這裡）  →  LLM 選表（table_filter）  →  KMB 補橋（schema_graph）

相似度負責「高召回地收斂候選」，不負責決定要幾張表 —— 那件事它做不到，
因為它看得到「這題關於客戶」，看不到「算總消費必須 JOIN orders」。決定
要哪幾張表的是 table_filter 那一層（為什麼、量了什麼，寫在該檔開頭）。
最後 KMB 補上橋接表：問「陳先生買了什麼手機」不會提到 order_items，
橋接表在問題裡沒有任何字詞會命中，只能靠外鍵結構找。

下面關於 K 與動態門檻的實測數字仍然成立，而且正是它們指出固定 K 是死路 ——
現在 K 只在 LLM 那一層失效時當退路用。

只用 dense，不做 hybrid。這是在真實 schema 的 139 題上量的，不是照抄先導實驗
（先導用 21 張合成表宣稱 K=3 有 95% 召回，真實資料是 87.1% —— 差很多）：

              K=2     K=3     K=4     K=6      （+KMB 之後的召回）
  Dense      79.9%   87.1%   91.4%   93.5%
  RRF hybrid 74.8%   81.3%   89.2%   92.8%
  BM25       69.1%   77.7%   83.5%   94.2%
每一個 K，加 BM25 做 RRF 都比純 dense 差。中文字元 bigram 在 246~864 字的短
文件上雜訊太大，詞彙重疊多半來自機率而非語意。

另一個反覆驗證到的規律：**短文件加字會稀釋鑑別力**。三次都是同一個方向 ——
  LLM 合成的典型問法   K=3 87.1% → 82.7%
  enum 中文狀態值對照   K=3 87.1% → 86.3%
  只留精確簡潔的註解   K=3 87.1%（最佳）
所以檢索文件要的是「描述精準」，不是「詞彙量大」。要補的是 TABLE_COMMENT
本身的品質（見 tools/sync_table_comments.py），不是往文件裡疊東西。

失效時一律回傳「全部的表」而不是拋例外 —— 檢索失敗應該退化成「Prompt
變長」，不該退化成「答不出來」。
"""
import hashlib
import json
import math
import os
import threading
import zlib

import requests
from loguru import logger as log
from sqlalchemy import text

from langgraph_sql.config import MYSQL_URI, NVIDIA_API_KEY
from langgraph_sql.utils.db_manager import get_db_manager
from langgraph_sql.utils.schema_graph import find_join_path
from langgraph_sql.utils.table_filter import (
    filter_tables, get_candidate_n, get_table_briefs,
)

EMBED_URL = "https://integrate.api.nvidia.com/v1/embeddings"
EMBED_MODEL = "nvidia/nemotron-3-embed-1b"

# LLM 選表那一層失效時的退路（也是 eval_retrieval 量基準時用的設定）。
# 多一個錨點在 KMB 裡不只是多一張表 —— 它會把整條路徑拉進來，
# 實測一個假錨點會多帶 1 到 4 張表，所以退路寧可保守。
DEFAULT_TOP_K = 3

# 動態門檻（Elbow Method）—— 實測比固定 K 差，預設關閉。
#
# 這個想法很自然：需要幾張表本來就因題而異，讓分數自己決定切在哪裡。
# 但實測 139 題完全不成立：
#     r=0.85  召回 60.4% / 平均 1.6 個錨點
#     r=0.75  召回 69.8% / 平均 2.3 個錨點
#     K=6     召回 95.0% / 6 個錨點
# 原因是餘弦分數根本沒有 elbow —— 多數題目第 2 名就已經掉到第 1 名的 85%
# 以下，但「分數陡降」不等於「需要的表比較少」。門檻適應的方向是錯的。
# 保留這個實作是為了記錄這個負面結果，不是為了用它。
DEFAULT_RATIO: float | None = None
MAX_ANCHORS = 6

# 向量快取。key 是「表文件」的雜湊，schema 一改雜湊就變，只有變動的表會重算。
_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ".table_vectors.json",
)

_docs: dict[str, str] | None = None
_vectors: dict[str, list[float]] | None = None
_doc_lock = threading.Lock()
_vec_lock = threading.Lock()


_TABLE_DOCS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "utils", "table_docs.yaml",
)


def _load_synthesized_docs() -> dict[str, str]:
    """
    載入 tools/gen_table_docs.py 產生的「用途 + 典型問法」（預設不存在）。

    ⚠️ 實測結論：這個做法弊大於利，檔案已移除，這裡只保留載入路徑以便日後
    要再實驗。原本的推論是「表描述缺動詞（營收、賣出）所以檢索不到
    order_items」，方向沒錯，但真正的原因是表註解太爛 —— order_items 的
    TABLE_COMMENT 當時只有「訂單明細表」五個字，把表名複述一遍而已。

    把表註解寫好之後，兩者對照（139 題）：
                        K=3      K=4      K=5
      好註解 + 合成問法   82.7%    87.1%    92.1%
      只有好註解         87.1%    91.4%    92.1%
    合成問法在 K≤4 反而低 4 個百分點。原因是它給每張表都灌進通用電商語彙，
    讓所有文件對任何查詢都變得有點像，鑑別力反而下降 —— 檢索層的稀釋效應，
    和 Prompt 稀釋是同一回事。

    結論：該補的是 TABLE_COMMENT（權威、在資料庫裡、同時餵給 DDL 與檢索），
    不是在旁邊生一份 LLM 產物。見 tools/sync_table_comments.py。
    """
    if not os.path.exists(_TABLE_DOCS_PATH):
        return {}
    try:
        import yaml
        log.info("[Retriever] 偵測到 table_docs.yaml —— 實測此設定召回較低，確認是否有意為之")
        with open(_TABLE_DOCS_PATH, encoding="utf-8") as f:
            return {k.lower(): v for k, v in (yaml.safe_load(f) or {}).items()}
    except Exception as e:
        log.warning(f"[Retriever] table_docs.yaml 讀取失敗（{type(e).__name__}），只用結構描述")
        return {}


def build_table_documents() -> dict[str, str]:
    """
    每張表做成一段可被語意檢索的文字 —— **只有表名 + 表註解，不含欄位清單**。

    註解就是全部的訊號：表名和欄位名都是英文，中文問題唯一能命中的是註解。
    實測過 order_items 的 TABLE_COMMENT 原本只有「訂單明細表」五個字（把表名
    複述一遍），而它正是檢索唯一持續找不到的表；把註解寫成「一張訂單買了哪些
    商品…商品銷量、營收、熱賣排行都要用這張表計算」之後就找得到了。

    為什麼不放欄位清單（139 題實測，嵌入是確定性的，沒有跑次變異）：

        變體                     Top-1   寬表誤選  候選@12   K=3     K=4
        只有表註解（現行）         84.9%   9/139   95.7%  85.6%  90.6%
        註解 + 鍵 + 非鍵前 12 欄   81.3%  13/139   97.1%  85.6%  90.6%
        註解 + 鍵                83.5%  10/139   97.8%  84.2%  89.2%
        註解 + 全部欄位           76.3%  24/139   97.1%  86.3%  92.1%

    欄位清單唯一的貢獻是「候選召回」高 1.4pp，而那一段在表 <= CANDIDATE_N 時
    根本沒作用。代價則是把文件長度變成表寬的函數：customer_profiles 有 57 欄，
    文件 1,828 字元、其他表中位數 173，結果它對幾乎所有問題都排前面 ——
    139 題有 24 題把它選為 Top-1，而沒有一題需要它。

    § 稀釋定律（ARCHITECTURE.md §2.10）說「往所有文件加字，大家都變像」；
      這裡是它的鏡像：**往一份文件加字，那一份到處都贏**。
      根治的辦法不是替寬表設一個裁剪上限（那只是把 24 壓到 13，還多一個
      憑感覺的常數），而是讓文件長度**與表寬無關** —— 只放表註解就做到了。

    誤差獨立性也量過了。檢索文件與 LLM 候選目錄現在讀完全相同的文字，
    照 §2.14 的教訓應該擔心「錯得一致、∪ Top-1 這個保險失效」，但實測沒有：
    兩者救回的是同樣 2 題，淨度還更好（87.6% vs 87.0%）。
    **誤差獨立來自機制不同（餘弦 vs 推理），不是來自輸入不同。**

    也刻意不放：欄位型別（實測 Top-1 88.5% → 87.8%，中文問題不會跟
    DECIMAL(10,2) 相似）、enum 中文狀態值對照（K=3 87.1% → 86.3%）、
    LLM 合成的典型問法（K=3 87.1% → 82.7%，見 _load_synthesized_docs）。
    型別與 enum 仍然會進 Prompt，只是走 DDL 與 get_enum_text()。

    要提升檢索只有一條路：**把 TABLE_COMMENT 寫好**（見
    tools/sync_table_comments.py）。它是權威來源，同時餵給 DDL、檢索向量
    與 LLM 候選目錄，一次修好三個地方。
    """
    global _docs
    if _docs is not None:
        return _docs

    with _doc_lock:
        if _docs is not None:
            return _docs
        synth = _load_synthesized_docs()
        # synth 預設不存在；保留只為記錄那個負面結果
        _docs = {t: (f'{synth[t]}\n\n表 {t}：{brief}' if t in synth
                     else f'表 {t}：{brief}')
                 for t, brief in get_table_briefs().items()}
        return _docs


def _doc_hash(doc: str) -> str:
    return hashlib.sha256(doc.encode("utf-8")).hexdigest()[:16]


def _embed(texts: list[str], kind: str) -> list[list[float]]:
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


def get_table_vectors() -> dict[str, list[float]]:
    """
    表向量，帶磁碟快取。快取以「文件雜湊」為 key，所以改了欄位註解會自動失效，
    而重跑一次程式不會重算 —— 每查一次都重嵌 21 張表是不能接受的。
    """
    global _vectors
    if _vectors is not None:
        return _vectors

    with _vec_lock:
        if _vectors is not None:
            return _vectors

        docs = build_table_documents()
        cache: dict[str, list[float]] = {}
        if os.path.exists(_CACHE_PATH):
            try:
                with open(_CACHE_PATH, encoding="utf-8") as f:
                    cache = json.load(f)
            except Exception as e:
                log.warning(f"[Retriever] 向量快取讀取失敗（{type(e).__name__}），重新計算")

        stale = [t for t, d in docs.items() if _doc_hash(d) not in cache]
        if stale:
            log.info(f"[Retriever] 需要嵌入 {len(stale)} 張表的描述")
            vecs = _embed([docs[t] for t in stale], "passage")
            for table, vec in zip(stale, vecs):
                cache[_doc_hash(docs[table])] = vec
            try:
                with open(_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(cache, f)
            except Exception as e:
                log.warning(f"[Retriever] 向量快取寫入失敗（{type(e).__name__}），下次會重算")

        _vectors = {t: cache[_doc_hash(d)] for t, d in docs.items()}
        return _vectors


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def rank_tables(query: str) -> list[tuple[str, float]]:
    """所有表依語意相似度排序，高分在前。失敗時回傳空陣列。"""
    try:
        vectors = get_table_vectors()
        qvec = _embed([query], "query")[0]
    except Exception as e:
        log.warning(f"[Retriever] 嵌入失敗（{type(e).__name__}: {e}），退回全表")
        return []
    # 分數並列時以表名排序，確保同一個問題每次得到同一組錨點
    return sorted(((t, _cosine(qvec, v)) for t, v in vectors.items()),
                  key=lambda x: (-x[1], x[0]))


def pick_anchors(
    ranked: list[tuple[str, float]],
    ratio: float | None = DEFAULT_RATIO,
    top_k: int = DEFAULT_TOP_K,
    max_k: int = MAX_ANCHORS,
) -> list[str]:
    """
    從排名挑錨點。ratio 給 None 就退回固定 top_k。

    為什麼要動態門檻：需要的錨點數本來就因題而異。「有幾筆退款完成？」只要
    一張表，「各商品類別各賣出多少件？」要三張。固定 K=3 對前者多帶兩張噪音、
    對後者剛好把第 4 名的 order_items 切掉（實測分數 0.3742 vs 0.3658，
    差 0.008 就掉出去）。

    門檻取「與第一名的相對比例」而不是絕對分數 —— 餘弦分數的絕對值隨問題長度
    與用詞浮動很大（實測第一名介於 0.29 到 0.42），絕對門檻沒有一個值是通用的。
    """
    if not ranked:
        return []
    if ratio is None:
        return [t for t, _ in ranked[:top_k]]

    floor = ranked[0][1] * ratio
    anchors = [t for t, s in ranked[:max_k] if s >= floor]
    return anchors or [ranked[0][0]]


def select_tables(
    query: str,
    top_k: int = DEFAULT_TOP_K,
    ratio: float | None = DEFAULT_RATIO,
    use_filter: bool = True,
) -> tuple[set[str], list[str]]:
    """
    回傳 (要放進 Prompt 的表集合, 被選為錨點的表)。

    三段漏斗：
      相似度  把候選收斂到 CANDIDATE_N 張（21 張表時等於全部，見 table_filter）
      LLM     從候選裡「選」出邏輯上必要的表 —— 這一段做相似度做不到的推理
      KMB     補上把錨點接起來的橋接表

    錨點 = LLM 的選擇 ∪ 相似度第 1 名。兩者錯誤型態互補：LLM 會漏掉問題裡
    明講的主體，相似度看不到「算營收要用訂單明細」這種結構必要性。實測
    只補第 1 名就到頂（99.3%），補到第 2 名召回不再上升、淨度掉 24 個百分點。

    use_filter=False 退回純相似度 Top-K —— 給 eval_retrieval 量基準用，
    也是 LLM 那一層整個不可用時的行為。

    刻意不對 LLM 選的張數設上限（MAX_ANCHORS 只作用在動態門檻那條路）。
    139 題實測分佈是 1 張 55 題、2 張 56 題，最多 6 張，沒有失控的跡象；
    而萬一模型真的把整個資料庫選進來，結果就是退回全表 —— 那本來就是這一層
    設計好的降級行為。反過來，截斷會從一份沒有重要性排序的清單裡砍掉尾巴，
    有機會砍掉真正需要的表，那是在防一個不存在的問題時製造一個真的問題。

    任何一段失效都只降級不中斷：嵌入失敗回傳全部的表，LLM 失敗回傳純相似度
    的結果。退化的結果是 Prompt 變長，不是答不出來。
    """
    all_tables = sorted(build_table_documents())
    ranked = rank_tables(query)

    # 嵌入 API 掛掉時仍然要走 LLM 那一層：它讀的是表註解，不需要向量。
    # 少了相似度只是少了排序先驗與 ∪ 第 1 名的保險，不是整層失效 ——
    # 實測嵌入端點會偶發 502，那時候直接丟 21 張表進 Prompt 太浪費。
    top_n = get_candidate_n(len(all_tables))
    candidates = [t for t, _ in ranked[:top_n]] if ranked else all_tables
    anchors = pick_anchors(ranked, ratio=ratio, top_k=top_k) if ranked else []

    source = "相似度" if ranked else "無"
    if use_filter:
        # 候選清單**打散順序**再給 LLM，不照相似度排名。
        #
        # §2.2 原本說照排名給是「免費給 LLM 一個先驗」。實測（§7.12）那個先驗
        # 是淨負的：固定順序 n=3 錨點召回 94.8/92.9/93.5、+KMB 97.4/96.8/97.4；
        # 打散 n=2 是 98.1/96.1、+KMB 99.4/98.1 —— 兩組區間完全不重疊。
        # 位置偏誤影響的不只是「排後面被忽略」，固定順序還會讓同一組偏誤每次
        # 都重現，於是錯的那幾題**穩定地錯**。
        #
        # seed 用問題的雜湊：同一題永遠得到同一種順序（可重現、可除錯），
        # 不同題的順序不同（不會固化成另一個新的偏誤）。
        picked = filter_tables(query, candidates,
                               shuffle_seed=zlib.crc32(query.encode("utf-8")))
        if picked:
            # dict.fromkeys 保序去重：LLM 的選擇在前，相似度第 1 名補在後
            anchors = list(dict.fromkeys(picked + ([ranked[0][0]] if ranked else [])))
            source = "LLM∪相似度#1" if ranked else "LLM"

    if not anchors:
        log.warning("[Retriever] 相似度與 LLM 選表都失效，退回全表")
        return set(all_tables), []

    # source 要印出來：這一層降級是靜默的（退回相似度只是少一點召回，
    # 端到端看不出來），不印就可能整層壞掉幾個月都沒人發現
    tables = find_join_path(anchors)
    log.info(f"[Retriever] 錨點({source}) {anchors} → {len(tables)} 張表 {sorted(tables)}")
    return tables, anchors
