"""
table_filter 的回歸測試 —— 鎖住「解析 LLM 輸出」這一段
====================================================================
為什麼只測解析、不測選表品質：

選表品質是統計性質的，要用 139 題量（`eval_retrieval.py` 與檢索實驗），
單元測試量不了，而且每跑一次都要打 API。這裡測的是另一件事 ——
**解析失敗是靜默的**。

`filter_tables` 解析不出東西時回傳空陣列，呼叫端會安靜地退回相似度 Top-K。
那正是我們花力氣換掉的舊行為，但 log 上只有一行 warning，端到端正確率
也只會掉一兩個百分點 —— 完全可能上線幾個月都沒人發現這一層其實沒在運作。
所以解析必須被鎖住。

（實測過一次真的踩到：6 條並行時 NIM 會回 503，139 題有 13 題拿到空回覆
  而靜默降級。當時是因為離線腳本印了「退回相似度 N 題」才看見。）

執行：python test_script/test_table_filter.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from langgraph_sql.utils.table_filter import _parse_selection  # noqa: E402

ALLOWED = {"orders", "order_items", "products", "customers", "refunds"}

# (說明, 模型輸出, 期望解析結果)
CASES = [
    ("標準輸出",
     '[{"table": "orders", "reason": "訂單張數"}, '
     '{"table": "order_items", "reason": "算銷量"}]',
     ["orders", "order_items"]),

    ("包在 markdown code fence 裡",
     '```json\n[{"table": "products", "reason": "商品單價"}]\n```',
     ["products"]),

    ("前後有多餘的說明文字",
     '好的，我來分析。\n[{"table": "refunds", "reason": "退款金額"}]\n以上。',
     ["refunds"]),

    # reasoning 模型會先寫草稿陣列再給結論，取最後一個才對
    ("有草稿陣列在前面，要取最後一個",
     '先考慮 [{"table": "orders", "reason": "草稿"}]\n'
     '結論：[{"table": "customers", "reason": "問的是客戶"}]',
     ["customers"]),

    # 幻覺的表名放行下去，KMB 會拿一個圖上不存在的節點去找路徑
    ("含不存在的表名，必須濾掉",
     '[{"table": "orders", "reason": "x"}, {"table": "order_detail", "reason": "y"}]',
     ["orders"]),

    ("大小寫與空白要正規化",
     '[{"table": " Orders ", "reason": "x"}, {"table": "PRODUCTS", "reason": "y"}]',
     ["orders", "products"]),

    ("重複的表只留一個",
     '[{"table": "orders", "reason": "a"}, {"table": "orders", "reason": "b"}]',
     ["orders"]),

    ("純字串陣列（沒有 reason 欄位）也要能吃",
     '["orders", "products"]',
     ["orders", "products"]),

    # reason 是模型自由書寫的中文，出現 ']' 不是罕見情況
    ("reason 裡面含有 ] 字元",
     '[{"table": "orders", "reason": "狀態 [COMPLETED] 的訂單"}, '
     '{"table": "products", "reason": "取品名"}]',
     ["orders", "products"]),

    ("巢狀陣列不能讓解析壞掉",
     '[{"table": "refunds", "reason": "退款", "cols": ["amount", "reason"]}]',
     ["refunds"]),

    # 以下全部必須回空陣列 —— 呼叫端靠「空」來判斷要不要退回相似度
    ("完全不是 JSON", "我覺得應該用 orders 和 products 這兩張表。", []),
    ("JSON 壞掉", '[{"table": "orders", "reason":]', []),
    ("空陣列", "[]", []),
    ("全部都是幻覺表名", '[{"table": "sales", "reason": "x"}]', []),
    ("空字串（API 回 200 但內容空白）", "", []),
]


def main() -> int:
    failed = 0
    for label, raw, expected in CASES:
        got = _parse_selection(raw, ALLOWED)
        if got != expected:
            print(f"❌ {label}\n   期望 {expected}\n   得到 {got}")
            failed += 1

    if failed:
        print(f"\ntable_filter 測試失敗 {failed}/{len(CASES)} 項")
        return 1
    print(f"table_filter 測試全數通過（{len(CASES)} 項）")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
