"""
表權重的漂移偵測 —— 新表出現時不會被靜默漏掉
====================================================================
schema_graph._TABLE_COST 是一份人工清單：把「行為/意圖類」的表調貴，避免
拓撲最短路抄近道（問「買了什麼」卻走 browse_logs）。清單只有 4 筆，現在
維護得動；表長到 100 張就維護不動了，而漏掉一張的後果是靜默的錯答。

這支程式用語意探針替每張表打分（行為探針 − 交易探針），跟設定值對照，
不一致就報警。它是偵測器，不是推導器 —— 這個區別是量出來的：

  cart_items  +0.177 │ 三張典型行為表，與後面差一個數量級，
  carts       +0.164 │ 新表若是日誌類會落在這一區，偵測非常可靠
  browse_logs +0.151 │
  ─────────────────────────────────────────────────────
  categories  +0.068   偽陽性，但度數 1 是葉節點 —— 權重只在「被穿過」時
                       才有作用，葉節點永遠不會被穿過，所以無害
  reviews     +0.033   人工標為貴
  customers   +0.028   度數 5 的樞紐，標貴會是災難
  ─────────────────────────────────────────────────────
reviews 與 customers 只差 0.005，任何抓得到前者的門檻都逼近後者。中間地帶
交給人判斷，兩端交給探針 —— 這才是這個訊號能可靠承擔的角色。

值得記的一點：這個訊號做到了結構推導做不到的事。browse_logs 與 shipments
的出度、入度、有無時間欄完全相同，用 INFORMATION_SCHEMA 分不開，
而探針差 0.31（+0.151 vs −0.159）。

執行：python tools/check_table_costs.py     # 不一致時回傳非 0
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from loguru import logger as log  # noqa: E402

from langgraph_sql.utils.schema_graph import (  # noqa: E402
    _DEFAULT_TABLE_COST,
    get_join_graph,
    table_cost,
)
from langgraph_sql.utils.table_retriever import (  # noqa: E402
    _cosine,
    _embed,
    build_table_documents,
    get_table_vectors,
)

BEHAVIOURAL_PROBE = (
    "使用者的瀏覽紀錄、點擊、停留、加入購物車、評論等行為軌跡，"
    "屬於還沒有成交的意圖資料，不代表實際購買"
)
TRANSACTIONAL_PROBE = (
    "已經成交的交易紀錄：訂單、訂單明細、付款、出貨、退款，"
    "代表實際發生的商業交易"
)

# 高於這條線且能被穿過的表，幾乎確定是行為類。定在 0.10 是因為三張已知的
# 行為表都在 0.15 以上，而第一個偽陽性在 0.068 —— 中間有很大的空隙。
HIGH = 0.10
# 低於這條線卻被標為昂貴的，值得回頭看一眼。
LOW = -0.05


def main() -> int:
    build_table_documents()
    vectors = get_table_vectors()
    graph = get_join_graph()
    log.remove()

    pb, pt = _embed([BEHAVIOURAL_PROBE, TRANSACTIONAL_PROBE], "query")
    scored = sorted(
        ((t, _cosine(pb, v) - _cosine(pt, v), len(graph.get(t, ()))) for t, v in vectors.items()),
        key=lambda r: -r[1],
    )

    problems: list[str] = []
    for table, score, degree in scored:
        costly = table_cost(table) > _DEFAULT_TABLE_COST
        # 葉節點（度數 < 2）永遠不會被當成中途站，權重對它沒有意義，不必報警
        if degree < 2:
            continue
        if score >= HIGH and not costly:
            problems.append(
                f"{table} 語意上像行為/意圖表（{score:+.3f}）且度數 {degree} 可被穿過，"
                f"但成本仍是預設值 —— 它可能正在被當成捷徑")
        if score <= LOW and costly:
            problems.append(
                f"{table} 語意上像交易表（{score:+.3f}）卻被標為昂貴 —— 確認是否仍需要")

    print(f"{'表':<20} {'行為−交易':>10} {'度數':>5} {'成本':>5}")
    print("-" * 44)
    for table, score, degree in scored:
        mark = " *" if table_cost(table) > _DEFAULT_TABLE_COST else ""
        print(f"{table:<20} {score:>+10.3f} {degree:>5} {table_cost(table):>5}{mark}")

    if problems:
        print(f"\n發現 {len(problems)} 項不一致:")
        for p in problems:
            print(f"  - {p}")
        print("\n請人工判斷後更新 schema_graph._TABLE_COST，"
              "並在 test_script/test_schema_graph.py 補上對應的路徑斷言。")
        return 1

    print("\n設定與語意探針一致，沒有可被穿過的行為表漏標。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
