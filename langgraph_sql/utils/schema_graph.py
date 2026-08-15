"""
Schema Graph — 表與表之間的外鍵關聯
=====================================
外鍵目前只以純文字形式（DDL 裡的 FOREIGN KEY 行）餵給 LLM，程式端完全沒有
可用的關聯結構 —— schema_parser 剖析 DDL 時會明確跳過那幾行。這個模組把
關聯讀成資料結構，來源同樣是 MySQL 自己（INFORMATION_SCHEMA），理由與
schema_registry 一致：手寫 YAML 會漂移，regex 失效時是靜默的。

為什麼需要它：schema 一大就必須只把「相關的表」放進 Prompt，而「相關」
不能只靠語意相似度決定。「陳先生上個月買了哪些手機」語意上只命中
customers 與 products，但少了 orders、order_items 這兩張橋接表就接不起來，
而問題裡沒有任何字詞會命中它們。橋接表要靠關聯結構找，不是靠語意。

find_join_path() 用的是 Steiner Tree 的最短路徑啟發式（SPH，2-近似）。
在 20 張表的規模，近似解與最佳解幾乎必然相同，不值得引入圖論套件。
"""
import threading
from collections import deque
from collections.abc import Iterable
from typing import NamedTuple

from loguru import logger as log
from sqlalchemy import text

from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager


class ForeignKey(NamedTuple):
    """一條外鍵：child.column → parent.ref_column。"""
    table: str
    column: str
    ref_table: str
    ref_column: str


_FOREIGN_KEY_SQL = """
SELECT TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
WHERE TABLE_SCHEMA = DATABASE()
  AND REFERENCED_TABLE_NAME IS NOT NULL
ORDER BY TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME
"""

_foreign_keys: list[ForeignKey] | None = None
_join_graph: dict[str, set[str]] | None = None
_lock = threading.Lock()


def get_foreign_keys() -> list[ForeignKey]:
    """取得所有外鍵，全部小寫。首次呼叫時查 DB 並快取。"""
    global _foreign_keys
    if _foreign_keys is not None:
        return _foreign_keys

    with _lock:
        if _foreign_keys is not None:
            return _foreign_keys
        try:
            db = get_db_manager(MYSQL_URI)
            with db.engine.connect() as conn:
                rows = conn.execute(text(_FOREIGN_KEY_SQL)).fetchall()
            _foreign_keys = [
                ForeignKey(t.lower(), c.lower(), rt.lower(), rc.lower())
                for t, c, rt, rc in rows
            ]
        except Exception as e:
            log.error(f"[Schema] 無法讀取外鍵（{type(e).__name__}: {e}），"
                      "join path 搜尋將失效")
            _foreign_keys = []

        if not _foreign_keys:
            # 空的外鍵集合不是「這個資料庫沒有關聯」，多半是引擎不是 InnoDB
            # 或建表時漏掉約束。它會讓 find_join_path 靜默退化成「原樣回傳」，
            # 所以在這裡就喊出來。
            log.warning("[Schema] INFORMATION_SCHEMA 沒有回報任何外鍵 —— "
                        "確認資料表引擎為 InnoDB 且 FOREIGN KEY 有實際建立")
        else:
            log.info(f"[Schema] 讀入 {len(_foreign_keys)} 條外鍵")
        return _foreign_keys


def get_join_graph() -> dict[str, set[str]]:
    """
    把外鍵攤成無向鄰接表 {table: {鄰接的 table}}。

    無向是刻意的：JOIN 兩邊都可以當起點，「從商品找客戶」和「從客戶找商品」
    走的是同一條路。自我參照的外鍵（如 employees.manager_id → employees）
    不會產生自環，因為對搜尋路徑沒有幫助。
    """
    global _join_graph
    if _join_graph is not None:
        return _join_graph

    with _lock:
        if _join_graph is not None:
            return _join_graph
        graph: dict[str, set[str]] = {}
        for fk in get_foreign_keys():
            if fk.table == fk.ref_table:
                continue
            graph.setdefault(fk.table, set()).add(fk.ref_table)
            graph.setdefault(fk.ref_table, set()).add(fk.table)
        _join_graph = graph
        return _join_graph


def _shortest_paths_from(graph: dict[str, set[str]], start: str) -> dict[str, list[str]]:
    """BFS：回傳 start 到每個可達節點的最短路徑（含頭尾）。"""
    paths: dict[str, list[str]] = {start: [start]}
    queue: deque[str] = deque([start])
    while queue:
        node = queue.popleft()
        # sorted 不是為了效率而是為了決定性：同長度的路徑必須每次都選同一條，
        # 否則同一個問題會時而多帶一張表、時而少帶一張。
        for nxt in sorted(graph.get(node, ())):
            if nxt not in paths:
                paths[nxt] = paths[node] + [nxt]
                queue.append(nxt)
    return paths


def find_join_path(anchors: Iterable[str]) -> set[str]:
    """
    給定一組錨點表，回傳「把它們全部連起來所需的最小表集合」（含中間的橋接表）。

    演算法是 Steiner Tree 的最短路徑啟發式：先放進第一個錨點，接著反覆挑出
    「離目前這棵樹最近的錨點」，把整條路徑併進來，直到所有錨點都連上。

    錨點若彼此不連通（資料庫本來就分成好幾塊），會照樣把孤立的錨點放進結果
    並發出警告 —— 少給模型一張它明確需要的表，比多給一張糟糕得多。

    ⚠️ 已知限制：最短路徑不等於語意正確的路徑。在 24 條邊的合成圖上實測，
    customers → products 的最短路是 browse_logs（瀏覽紀錄，3 跳），而不是
    orders → order_items（購買紀錄，4 跳）。問「買了什麼」卻走瀏覽紀錄，
    拓撲對、語意錯，而且錯得無聲無息。目前 4 張表只有唯一一條路所以碰不到。
    擴表之後必須處理，兩個方向：
      1. 錨點不能只從名詞來。「買」這個動詞就該把 orders 拉成錨點，
         錨點齊全時最短路自然會走對 —— 這代表表級檢索的價值不只是找實體表，
         更是從動詞找出關係表。
      2. 邊要有權重。log / 行為類的表（browse_logs、search_logs）調高成本，
         交易主線（orders、payments）調低。
    """
    graph = get_join_graph()
    # dict.fromkeys 去重且保序，讓「第一個錨點」是可預測的
    wanted = list(dict.fromkeys(t.lower() for t in anchors))
    if len(wanted) <= 1:
        return set(wanted)

    connected = [t for t in wanted if t in graph]
    isolated = [t for t in wanted if t not in graph]
    if not connected:
        return set(wanted)

    tree: set[str] = {connected[0]}
    remaining: set[str] = set(connected[1:])

    while remaining:
        best: tuple[int, list[str]] | None = None
        for anchor in sorted(remaining):
            paths = _shortest_paths_from(graph, anchor)
            for target in sorted(tree):
                path = paths.get(target)
                if path is not None and (best is None or (len(path), path) < best):
                    best = (len(path), path)
        if best is None:
            log.warning(f"[Schema] 這些錨點與 {sorted(tree)} 之間沒有外鍵路徑: "
                        f"{sorted(remaining)}（照樣納入，但 JOIN 條件要模型自己想）")
            tree |= remaining
            break
        tree.update(best[1])
        remaining -= tree

    if isolated:
        log.warning(f"[Schema] 這些表沒有任何外鍵，無法納入路徑搜尋: {isolated}")
        tree |= set(isolated)

    return tree


def format_join_hints(tables: Iterable[str]) -> str:
    """
    列出給定表集合「內部」的外鍵條件，一行一條，供組裝精簡 DDL 時附上。

    只列集合內部的關聯：指向已被剪掉的表的外鍵，寫出來只會誘導模型去 JOIN
    一張它看不到 schema 的表。
    """
    scope = {t.lower() for t in tables}
    return "\n".join(
        f"  {fk.table}.{fk.column} = {fk.ref_table}.{fk.ref_column}"
        for fk in get_foreign_keys()
        if fk.table in scope and fk.ref_table in scope
    )
