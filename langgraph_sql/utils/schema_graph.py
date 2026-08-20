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

find_join_path() 用的是 KMB（Kou–Markowsky–Berman）近似演算法：
  1. 以每個錨點為起點各跑一次 Dijkstra —— 只跑一次，共 k 次搜尋。
  2. 把整張圖丟掉，只留錨點兩兩之間的成本，得到一張 k 個點的「距離矩陣」。
  3. 在這張虛擬圖上跑 MST，選出把所有錨點連起來最便宜的骨架。
  4. 把骨架的每條虛擬邊展開回真實路徑，聯集就是要餵給模型的表集合。

與先前的 SPH（最短路徑啟發式）近似保證相同（皆為 2−2/k），換成 KMB 是為了
搜尋次數：SPH 在迴圈內對每個尚未連上的錨點重算一次全圖搜尋，是 O(k²) 次；
KMB 是 O(k) 次。20 張表時兩者都是微秒級，表數上去之後才有差別。

（課本版 KMB 還有第 5 步「對展開後的子圖再取一次 MST 並剪掉非終端葉節點」。
  這裡不需要：我們只要「表的集合」不要邊集合，聯集裡的環無害；而展開的路徑
  必然兩端都是錨點，不會產生非終端的葉節點。）

邊權：成本記在「表」上而不是邊上 —— 「經過 browse_logs 很貴」本來就是表的
性質，而且設定量從 O(邊) 降到 O(表)。見 _TABLE_COST。
"""
import heapq
import threading
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

# 踏進一張表要付的成本。預設 1，行為/紀錄類的表調高。
#
# 為什麼需要這個：拓撲上的最短路不等於語意上正確的路。browse_logs 與 reviews
# 都直接把 customers 與 products 連起來（2 跳），比真正的購買路徑
# orders → order_items（3 跳）更短。於是問「客戶買了什麼」會走瀏覽紀錄 ——
# 拓撲對、語意錯，而且沒有任何錯誤訊息。實測 #133：瀏覽最多的是洋芋片、
# 銷量最高的是泡麵、營收最高的是 MacBook Air，走錯路會自信地回答錯的商品。
#
# 成本要落在一個區間裡，太重與太輕都會壞掉：
#   下界 —— 要贏過購買路徑才算修好。customers→browse_logs→products 成本
#           3+1=4，customers→orders→order_items→products 是 1+1+1=3，
#           3 < 4，購買路徑勝出。成本 2 的話兩邊都是 3，並列，修不掉。
#   上界 —— 這些表被當成「橋樑」時還是得走得過去。問購物車裡有什麼商品，
#           carts→cart_items→products 是唯一語意正確的路（成本 3+1=4），
#           但繞 carts→customers→orders→order_items→products 只要 1+1+1+1=4。
#           成本 4 會讓繞路以 5<8 勝出 —— 實測就是這樣繞掉的。成本 3 讓兩者
#           並列，再由「跳數少者優先」的決勝規則選回正確的 3 節點短路徑。
# 也就是說 3 是唯一同時滿足兩邊的值，不是隨手挑的。
_DEFAULT_TABLE_COST = 1
_TABLE_COST: dict[str, int] = {
    "browse_logs": 3,   # 瀏覽 ≠ 購買
    "reviews": 3,       # 評論 ≠ 購買
    "carts": 3,         # 加入購物車 ≠ 購買
    "cart_items": 3,
    # ↓ 2026-08-20 第一波擴表（§11.3）。這三張正是這個守門員存在的理由 ——
    #   它們都直接把 customers 與 products 用 2 跳連起來，比購買路徑短，
    #   忘了調成本就會讓「客戶買了什麼」走到收藏或問答上。
    "wishlists": 3,     # 收藏 ≠ 購買
    "wishlist_items": 3,
    "product_qna": 3,   # 提問 ≠ 購買
    # ↓ 2026-08-20 第二波擴表（§11.5）。同樣是「行為 ≠ 購買」那一族。
    "search_logs": 3,              # 搜尋 ≠ 購買
    "campaign_clicks": 3,          # 點廣告 ≠ 購買
    "product_recommendations": 3,  # 演算法推薦 ≠ 實際共同購買，
                                   # 而且它是 products↔products，
                                   # 成本 1 會讓任兩個商品之間憑空多一條 2 跳捷徑
}



# 已檢視過、確認維持預設成本 1 的表。
#
# 為什麼要把「預設」也列出來（ARCHITECTURE.md §11.2）：
#   `table_cost()` 對沒列到的表回傳預設值，**新表因此永遠不會報錯** ——
#   加一張新的「瀏覽 / 收藏 / 加購物車」型別的表卻忘了調成本，KMB 會靜默
#   繞錯路，症狀是「拓撲對、語意錯」，沒有任何錯誤訊息。這與 §10.6 的
#   「DDL 沒重跑」是同一類病：**手維護的清單沒跟上新 schema，而且不會喊。**
#
#   把預設也明列出來之後，這個集合 ∪ _TABLE_COST 必須等於全庫表名，
#   由 tools/check_schema_pipeline.py 強制。加表就一定要來這裡做一次決定，
#   決定「1」也可以，但必須是決定過的 1，不是漏掉的 1。
_DEFAULT_COST_REVIEWED: frozenset[str] = frozenset({
    # 交易主線（最短路本來就該走這裡）
    "customers", "orders", "order_items", "products",
    "payments", "payment_methods", "refunds", "invoices", "payment_attempts",
    "shipments", "delivery_attempts", "addresses",
    # 商品維度
    "categories", "product_categories", "suppliers", "product_suppliers",
    "product_specs", "product_images", "product_price_history",
    "product_stock_snapshots", "product_bundles",
    # 倉儲
    "warehouses", "warehouse_transfers",
    # 促銷
    "promotions", "order_promotions", "promotion_rules",
    # 訂單週邊
    "order_status_history", "order_notes", "order_cancellations", "order_returns",
    # 客戶週邊
    "customer_profiles", "customer_contacts", "customer_login_logs",
    "support_tickets",
    # 評價與購物車週邊
    "review_replies", "cart_recovery_emails",
    "supplier_contracts",
    # --- 2026-08-20 第一波擴表：以下維持成本 1，逐一檢視過的理由 ---
    # 組織／門市：不在 customers↔products 之間，構不成假捷徑
    "departments", "employees", "order_assignments", "stores", "store_pickups",
    # 金流週邊：都掛在 orders 上，與購買同向
    "coupons", "coupon_redemptions", "gift_cards", "gift_card_transactions",
    "loyalty_points", "subscriptions",
    # 商品週邊：product_tags 只連 products 單邊，不構成跨側捷徑
    "product_tags",
    # 基礎設施（never_answered）：user_sessions 只連 customers 單邊
    "audit_log", "schema_migrations", "job_runs", "user_sessions",
    # --- 2026-08-20 第二波擴表：以下維持成本 1，逐一檢視過的理由 ---
    # 組織 ↔ 場域：不在 customers↔products 之間
    "warehouse_staff", "store_staff", "employee_shifts",
    # 商品變體：只連 products 單邊
    "product_variants", "variant_barcodes",
    # 行銷主檔：newsletters / campaigns 本身不連客戶，連的是它們的子表
    "newsletters", "newsletter_subscriptions", "campaigns",
    # 客服售後：都掛在 orders / order_returns / stores 上，與交易同向
    "faq_articles", "faq_votes", "return_shipments",
    "warranty_claims", "service_appointments",
    # 基礎設施（never_answered），全部不連業務主體
    "api_request_logs", "feature_flags", "cache_entries", "error_reports",
})


def table_cost(table: str) -> int:
    """經過這張表的成本。未列出的一律為預設值。"""
    return _TABLE_COST.get(table.lower(), _DEFAULT_TABLE_COST)


def unreviewed_tables(all_tables) -> list[str]:
    """回傳「既沒設成本、也沒被明確標為預設」的表 —— 加表時的守門員。"""
    known = set(_TABLE_COST) | _DEFAULT_COST_REVIEWED
    return sorted({t.lower() for t in all_tables} - known)


_foreign_keys: list[ForeignKey] | None = None
_join_graph: dict[str, set[str]] | None = None

# 兩份快取各用各的鎖。曾經共用一把，而 get_join_graph 會在鎖內呼叫
# get_foreign_keys —— threading.Lock 不可重入，那是必然的死鎖。
# 因為這個模組還沒被任何地方 import，一直沒被觸發。
_fk_lock = threading.Lock()
_graph_lock = threading.Lock()


def get_foreign_keys() -> list[ForeignKey]:
    """取得所有外鍵，全部小寫。首次呼叫時查 DB 並快取。"""
    global _foreign_keys
    if _foreign_keys is not None:
        return _foreign_keys

    with _fk_lock:
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

    # 必須在取鎖之前取外鍵：get_foreign_keys 自己會取 _fk_lock，
    # 順便也避免在持鎖期間跑一趟 DB 查詢。
    fks = get_foreign_keys()

    with _graph_lock:
        if _join_graph is not None:
            return _join_graph
        graph: dict[str, set[str]] = {}
        for fk in fks:
            if fk.table == fk.ref_table:
                continue
            graph.setdefault(fk.table, set()).add(fk.ref_table)
            graph.setdefault(fk.ref_table, set()).add(fk.table)
        _join_graph = graph
        return _join_graph


def _dijkstra_from(
    graph: dict[str, set[str]], start: str
) -> dict[str, tuple[int, list[str]]]:
    """
    帶權最短路：回傳 {節點: (成本, 路徑)}，路徑含頭尾。

    成本記在節點上 —— 每踏進一張表就付它的 table_cost()，起點不計。
    先前這裡是 BFS，那只在所有邊權都是 1 時才正確；要用權重就必須換成
    Dijkstra，否則權重會被靜默忽略。
    """
    dist: dict[str, int] = {start: 0}
    paths: dict[str, list[str]] = {start: [start]}
    visited: set[str] = set()
    heap: list[tuple[int, str]] = [(0, start)]

    while heap:
        cost, node = heapq.heappop(heap)
        if node in visited:
            continue
        visited.add(node)
        # sorted 與下面的三層比較都不是為了效率，而是為了決定性：成本並列時
        # 必須每次都選同一條路，否則同一個問題會時而多帶一張表、時而少帶一張。
        for nxt in sorted(graph.get(node, ())):
            if nxt in visited:
                continue
            cand_cost = cost + table_cost(nxt)
            cand_path = paths[node] + [nxt]
            known = dist.get(nxt)
            if known is None or (cand_cost, len(cand_path), cand_path) < (
                known, len(paths[nxt]), paths[nxt]
            ):
                dist[nxt] = cand_cost
                paths[nxt] = cand_path
                heapq.heappush(heap, (cand_cost, nxt))

    return {node: (dist[node], paths[node]) for node in dist}


def find_join_path(anchors: Iterable[str]) -> set[str]:
    """
    給定一組錨點表，回傳「把它們全部連起來所需的最小表集合」（含中間的橋接表）。

    演算法是 KMB，四個步驟見模組開頭的說明。權重讓「拓撲最短」與「語意正確」
    不再打架：問「客戶買了什麼」不會再被 browse_logs 抄近路。

    錨點若彼此不連通（資料庫本來就分成好幾塊），會照樣把孤立的錨點放進結果
    並發出警告 —— 少給模型一張它明確需要的表，比多給一張糟糕得多。

    註：錨點只從名詞來仍是個弱點。「買」這個動詞本來就該把 orders 拉成錨點，
    錨點齊全時連權重都不需要。權重是代理，動詞抽錨點才是精確解，但那要等
    檢索層能用動詞索引到關係表。兩者可以並存。
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

    # 步驟 1：多源最短路。每個錨點各跑一次 Dijkstra，只跑這一次。
    sp = {anchor: _dijkstra_from(graph, anchor) for anchor in connected}

    # 步驟 2+3：距離矩陣（度量閉包）＋ MST。整張圖在這裡被丟掉，只剩錨點
    # 兩兩之間的成本；Prim 在這張 k 點虛擬圖上挑出最便宜的骨架。
    # 步驟 4：每選一條虛擬邊就立刻展開回真實路徑併入結果。
    in_tree: list[str] = [connected[0]]
    tables: set[str] = {connected[0]}
    remaining: set[str] = set(connected[1:])

    while remaining:
        best: tuple[tuple[int, int, list[str]], str] | None = None
        for anchor in sorted(remaining):
            for target in sorted(in_tree):
                entry = sp[anchor].get(target)
                if entry is None:
                    continue
                cost, path = entry
                key = (cost, len(path), path)
                if best is None or key < best[0]:
                    best = (key, anchor)
        if best is None:
            log.warning(f"[Schema] 這些錨點與 {sorted(in_tree)} 之間沒有外鍵路徑: "
                        f"{sorted(remaining)}（照樣納入，但 JOIN 條件要模型自己想）")
            tables |= remaining
            break

        (_, _, path), anchor = best
        tables.update(path)
        in_tree.append(anchor)
        # 展開的路徑若順帶穿過其他還沒連上的錨點，它們已經連上了，
        # 不必再為它們挑一條邊 —— 只會讓結果多帶表。
        remaining -= tables

    if isolated:
        log.warning(f"[Schema] 這些表沒有任何外鍵，無法納入路徑搜尋: {isolated}")
        tables |= set(isolated)

    return tables


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
