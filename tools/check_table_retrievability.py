"""每張表在檢索裡的處境 —— 不問「哪一題錯了」，只問「這張表撈不撈得到」（零 LLM）

為什麼需要這一支（ARCHITECTURE.md §2.7m）：

    `products` 的 dense 平均排名是 **34.9**，對一張全庫第三常用的表來說荒謬 ——
    而它擺了兩個月沒人發現。發現的過程是：某一題失手 → 追失敗清單 →
    才看到 `#21` 的 `products` 排第 62 名。

    **那個過程本身不可靠。** 它需要有人盯著失敗、需要那張表剛好被某一題用到、
    還需要那一題剛好因此答錯（`#21` 一直是綠的，因為 KMB 把 `products`
    當橋補了回來 —— 缺陷被下游的救援機制遮住了）。

    這一支把它機械化：**逐表算「被 GT 需要時的 dense 排名」，不看對錯。**
    下一個 `products` 不必等它咬到某一題。

與既有兩支的分工（三支方向都不同，都要）：

    check_comment_blindspot   問句驅動：這一題的字面詞在不在該表註解裡
    check_comment_coverage    欄位驅動：欄位承載的概念群，表註解漏了哪些
    check_table_retrievability（本支）  **表驅動**：這張表被需要時，排第幾名

判準刻意綁在既有常數上，不引進新的可調參數（§10「停止調常數」）：

    紅燈  最差排名 > CANDIDATE_N   掉出候選層，後面兩段再準都救不回來
    黃燈  中位數   > CANDIDATE_N/2 還沒掉出去，但已經在後半段

**不給 KMB 的救援算分。** KMB 補回來是好事，但它遮住的正是這一支要看的東西 ——
§7.2 的「錨點 @40 = 98.9%、+KMB = 100.0%」就是這個遮蔽的原始紀錄。

**值索引（§2.7n）判得不一樣，理由要說清楚。** 它跟 KMB 有一個關鍵差別：
KMB 救到 `products` 是**偶然**的（它剛好在某條 join path 上，與「為什麼撈不到」
無關），值索引救到 `products` 是**對症**的（問句點名了商品的值，那正是失敗原因）。
所以：

    紅燈  判在 **production 組態**（`VALUE_BETA>0` 就含值索引）—— 紅燈要代表
          「今天真的救不回來」。判在純 dense 會讓它上線後永遠紅，
          而**被忽略的閘門比沒有閘門糟**。
    橙燈  production 過得去，但**純 dense 掉出候選** —— 這張表現在靠值索引撐著。
          不 exit 1，但一定點名：值索引只在 4.3% 的題有值可命中，
          題型一換這張表就裸露。

橙燈這一級就是 KMB 那個教訓的落實 —— 當年的問題不是「有救援」，
是**沒有人把救援印出來**，於是 98.9% 看起來像 100%。

用法：
    python tools/check_table_retrievability.py
    python tools/check_table_retrievability.py --all      # 連綠燈的表一起印
"""
import argparse
import io
import os as _os
import re
import statistics
import sys as _sys

_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)

import yaml
from loguru import logger as log

from langgraph_sql.utils.table_filter import get_candidate_n
from langgraph_sql.utils.table_retriever import (
    _cosine, _embed_query, get_table_vectors,
)
from langgraph_sql.utils.value_index import VALUE_BETA, value_hits

GT_PATH = _os.path.join(_ROOT, "eval_ground_truth.yaml")
# FROM/JOIN 後面的識別字。子查詢別名會被一起抓進來，靠「必須是真的表名」濾掉。
_TAB = re.compile(r"(?:FROM|JOIN)\s+`?([a-z_][a-z0-9_]*)`?", re.I)


def gt_required(tables: set[str]) -> dict[int, set[str]]:
    """{題號: 這題的 GT SQL 用到哪些真表}。alt_sql 也算 —— 那是同樣正確的寫法。"""
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    out = {}
    for e in gt:
        sql = " ".join(str(e.get(k) or "") for k in ("sql", "alt_sql"))
        req = {t.lower() for t in _TAB.findall(sql)} & tables
        if req:
            out[e["id"]] = req
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="連綠燈的表一起印")
    args = ap.parse_args()
    log.remove()

    vecs = get_table_vectors()
    tables = set(vecs)
    N = get_candidate_n()
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    req_by_q = gt_required(tables)

    ranks: dict[str, list[tuple[int, int]]] = {t: [] for t in tables}
    # 值索引之後的排名。**只用來標註「這是救回來的」，不參與紅黃燈判定。**
    vranks: dict[str, dict[int, int]] = {t: {} for t in tables}
    for e in gt:
        req = req_by_q.get(e["id"])
        if not req:
            continue
        qv = _embed_query(e["question"])
        sc = {t: _cosine(qv, vecs[t]) for t in tables}
        order = sorted(tables, key=lambda t: -sc[t])
        pos = {t: i + 1 for i, t in enumerate(order)}
        for t in req:
            ranks[t].append((pos[t], e["id"]))
        if VALUE_BETA:
            vs = dict(sc)
            for t, n in value_hits(e["question"]).items():
                if t in vs:
                    vs[t] += VALUE_BETA * min(n, 3)
            vorder = sorted(tables, key=lambda t: (-vs[t], t))
            vpos = {t: i + 1 for i, t in enumerate(vorder)}
            for t in req:
                vranks[t][e["id"]] = vpos[t]

    # 零筆結果有兩種意思 ——「查過了沒問題」與「根本沒查」。分開報（§8②）
    measured = {t: v for t, v in ranks.items() if v}
    unmeasured = sorted(t for t, v in ranks.items() if not v)

    red, orange, yellow, green = [], [], [], []
    for t, v in measured.items():
        rs = [r for r, _ in v]
        worst, worst_q = max(v)
        med = statistics.median(rs)
        row = (t, len(rs), med, worst, worst_q)
        # production 組態下的最差排名：值索引開著就算它，關著就等於純 dense
        worst_prod = max(vranks[t].values()) if VALUE_BETA and vranks[t] else worst
        if worst_prod > N:
            red.append(row)
        elif worst > N:
            orange.append(row)          # production 過，但純 dense 掉出候選
        elif med > N / 2:
            yellow.append(row)
        else:
            green.append(row)
    red.sort(key=lambda r: -r[3])
    orange.sort(key=lambda r: -r[3])
    yellow.sort(key=lambda r: -r[2])

    print(f"候選上限 CANDIDATE_N = {N}｜全庫 {len(tables)} 張表"
          f"｜GT 量得到 {len(measured)} 張、量不到 {len(unmeasured)} 張\n")

    def show(rows, title):
        if not rows:
            return
        print(title)
        for t, n, med, worst, wq in rows:
            # 值索引把這一題的最差排名救到哪 —— 標註用，不影響上面的燈號
            rescue = ""
            if VALUE_BETA and wq in vranks[t]:
                v = vranks[t][wq]
                if v < worst:
                    rescue = f"｜值索引救到 {v:3d}" + ("（仍在候選外）" if v > N else "")
            print(f"  {t:28s} 被需要 {n:3d} 次｜中位數 {med:5.1f}｜"
                  f"最差 {worst:3d}（#{wq}）{rescue}")
        print()

    show(red, f"🔴 最差排名掉出候選（> {N}）—— 後面兩段救不回來，"
              f"KMB 補得回也不算數:")
    show(orange, f"🟠 純 dense 掉出候選（> {N}），靠值索引撐著 —— "
                 f"值索引只在問句點名實際的值時作用，題型一換這張表就裸露:")
    show(yellow, f"🟡 中位數落在後半段（> {N // 2}）—— 還沒掉出去，但在邊緣:")
    if args.all:
        show(sorted(green, key=lambda r: -r[2]), "🟢 其餘:")

    allr = [r for v in measured.values() for r, _ in v]
    print(f"全庫 GT 表排名：中位數 {statistics.median(allr):.0f}｜"
          f"平均 {statistics.mean(allr):.2f}")
    print(f"候選召回 @{N}（純 dense，不含 KMB）："
          f"{sum(all(p <= N for p in [r for r, _ in ranks[t]]) for t in measured)}"
          f"/{len(measured)} 張表全數落在候選內")

    if VALUE_BETA:
        vall = [r for t in measured for r in vranks[t].values()]
        vok = sum(all(v <= N for v in vranks[t].values()) for t in measured)
        print(f"　　＋值索引（β={VALUE_BETA}）：平均 {statistics.mean(vall):.2f}｜"
              f"{vok}/{len(measured)} 張表全數落在候選內"
              f"　← **這是救援，不是燈號**（§2.7n）")
    else:
        print("　　（值索引關閉：VALUE_BETA=0，這一欄沒有量）")

    if unmeasured:
        # 沒有題目用到的表，這一支看不見 —— 說出來，不要讓它看起來像通過
        print(f"\n⚪ GT 從來不需要的 {len(unmeasured)} 張表**沒有被量到**"
              f"（不是通過）：{', '.join(unmeasured[:8])}"
              f"{' …' if len(unmeasured) > 8 else ''}")

    print()
    if red:
        print(f"❌ {len(red)} 張表被需要時會掉出候選層。"
              f"修法看 §2.7m：把表註解寫成「宣告自己回答什麼問題」，"
              f"舉例只能取自資料、不能取自題目。")
        return 1
    if orange:
        print(f"✅ 沒有表在 production 組態下掉出候選層，"
              f"但 **{len(orange)} 張靠值索引撐著**（橙燈，見上）——"
              f"擴表或換題型時第一個要回頭看的就是它們。")
    else:
        print(f"✅ 沒有表在被需要時掉出候選層（黃燈 {len(yellow)} 張，觀察用）")
    return 0


if __name__ == "__main__":
    _sys.stdout.reconfigure(encoding="utf-8")
    _sys.exit(main())
