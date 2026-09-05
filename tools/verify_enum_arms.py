"""兩臂差異的 assert —— 送背景之前一定要跑（§9.14 四之六）。

驗的是**行程內實際載入的東西**，不是檔名，也不是環境變數。
"""
import glob
import json
import os
import statistics
import sys

sys.path.insert(0, r"c:\Text-to-SQL")
import yaml  # noqa: E402

ROOT = r"c:\Text-to-SQL"
A_PATH = os.path.join(ROOT, "utils", "semantic_layer.yaml")
B_PATH = os.path.join(ROOT, "utils", "semantic_layer_enum_min.yaml")

A = yaml.safe_load(open(A_PATH, encoding="utf-8"))
B = yaml.safe_load(open(B_PATH, encoding="utf-8"))
Ae, Be = A["enum_fields"], B["enum_fields"]

# [1] 除了 enum_fields 之外，兩臂必須逐位元相同 —— 否則量到的不是 enum
for k in set(A) | set(B):
    if k == "enum_fields":
        continue
    assert A.get(k) == B.get(k), f"[1] 失敗：兩臂的 `{k}` 不同"
print(f"[1] 過：`ddl`／`business_rules`／`few_shot_examples` 等 {len(set(A)) - 1} 個區塊完全相同")

# [2] 兩臂的 enum_fields 必須真的不同，而且手寫那 6 個不能被動到
assert len(Ae) == 6 and len(Be) == 52, f"[2] 失敗：A={len(Ae)} B={len(Be)}"
for k in Ae:
    assert Ae[k] == Be[k], f"[2] 失敗：手寫項 `{k}` 被生成版蓋掉了"
print(f"[2] 過：A 臂 {len(Ae)} 項、B 臂 {len(Be)} 項，手寫 6 項逐字相同")

# [3] 防禦題探針：新增的 enum 不可以給防禦題任何落腳點
PROBES = ["運費", "freight", "shipping_fee", "郵資", "配送費", "物流費",
          "利潤", "毛利", "profit", "margin",
          "週轉", "turnover", "在庫", "期初", "期末", "inventory", "on_hand",
          "信用評分", "credit_score", "信評", "徵信"]
blob = yaml.dump({k: v for k, v in Be.items() if k not in Ae},
                 allow_unicode=True).lower()
hits = [p for p in PROBES if p.lower() in blob]
assert not hits, f"[3] 失敗：新增的 enum 命中防禦題探針 {hits}"
print(f"[3] 過：{len(PROBES)} 個防禦題探針在新增的 46 欄裡零命中")


def render(fields, tables):
    want = {t.lower() for t in tables}
    out = []
    for field, info in fields.items():
        if field.split(".")[0].lower() not in want:
            continue
        out.append(f"  {field} ({info.get('description','')})")
        for val, desc in (info.get("values") or {}).items():
            out.append(f"    - '{val}' = {desc}" if desc else f"    - '{val}'")
    return "\n".join(out)


# [4] 覆蓋面：多少題的 Prompt 位元不變（那半邊是免費的雜訊地板）
tabs: dict[int, set] = {}
rounds = 0
for fp in sorted(glob.glob(os.path.join(ROOT, "eval", "results", "eval_result_*.json"))):
    try:
        recs = json.load(open(fp, encoding="utf-8"))
    except Exception:
        continue
    if not isinstance(recs, list) or len(recs) < 300:
        continue
    rounds += 1
    for r in recs:
        tabs.setdefault(r.get("id"), set()).update(
            t.lower() for t in (r.get("retrieved_tables") or []))

changed, delta = [], []
for qid, ts in sorted(tabs.items()):
    a, b = render(Ae, ts), render(Be, ts)
    delta.append(len(b) - len(a))
    if a != b:
        changed.append(qid)

n = len(tabs)
print(f"[4] {rounds} 輪的 retrieved_tables 聯集，共 {n} 題")
print(f"    Prompt 會變的 {len(changed)} 題、**位元不變的 {n - len(changed)} 題**")
print(f"    新增字元：平均 {statistics.mean(delta):.0f}、中位 {statistics.median(delta):.0f}、"
      f"最大 {max(delta)}")

DEFENCE = [68, 70, 140, 141]
print(f"    防禦題落在會變的那半：{[q for q in DEFENCE if q in changed]}")

with open(os.path.join(ROOT, "eval", "enum_ab_changed.json"), "w",
          encoding="utf-8", newline="\n") as f:
    json.dump(changed, f)
print(f"    題號已寫入 eval/enum_ab_changed.json")
