# -*- coding: utf-8 -*-
"""全庫掃描：兩張共用母表的表，是否有「同一個事實」被寫成兩欄而值對不上。

閘門 [11] 是**問句驅動**且只看謂詞（布林／時間戳／列舉）。這一支補它的兩個洞：
  1. 沒有任何題目提到的配對（問句驅動看不到）
  2. 數值與字串欄位（min_amount / payment_terms / invoice_carrier / transit_days）

配對條件（兩者取聯集）：
  a) 欄名完全相同
  b) 註解共用一段中文，且型別同族

**這一支不是閘門，是探測器。** 它的誤報率很高（`status` vs `status`、
公分 vs 公釐都會上榜），所以不回傳非零、也不進 §6.3 的必跑清單 ——
它的用途是**擴表或改 schema 之後跑一次，人來看那份清單**。
判準是「有沒有哪個業務問題因此有兩個答案」，那一步機器做不了（§7.12）。

⚠️ 第一版報「乾淨」而它是錯的：字串比對因為兩批表 collation 不同會丟例外，
`except: continue` 把兩組 100% 對不上的欄位靜靜吃掉。所以現在**第一行就報
SQL 失敗幾組**，那個數字不是 0 就不准當成掃過了。

用法：.venv/Scripts/python.exe tools/check_synonym_columns.py
"""
import io
import os
import re
import sys
from collections import defaultdict

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
import yaml
from loguru import logger as log
from sqlalchemy import text
from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager
from langgraph_sql.utils.schema_graph import get_foreign_keys

log.remove()
sys.stdout.reconfigure(encoding="utf-8")

STOP = set("""時間 名稱 客戶 商品 訂單 資料 紀錄 數量 金額 是否 唯一 對應 這是 表示 為空 目前
建檔 流水 幾天 幾次 幾筆 使用 相關 一次 最近 最後 實際 完成 建立 那個 這個 其他 以及 或是
狀態 結果 類型 方式 原因 說明 備註 內容 編號 號碼 代碼 日期 天數 次數 人數 筆數 總額 單價
的時 時間 間， 已經 尚未 還沒 可能 表示 用的 否已 否為 否可 成時 立時 款時 貨時 請時 中為
與其 可與 訂閱中 訂單ID 客戶ID 商品ID 唯一ID 的時間 的名稱 建檔時間 流水號""".split())


def parse_ddl():
    raw = yaml.safe_load(io.open(os.path.join(_ROOT, "utils", "semantic_layer.yaml"),
                                 encoding="utf-8"))["ddl"]
    cols, cur = {}, None
    for line in raw.splitlines():
        m = re.search(r"CREATE TABLE\s+`?(\w+)`?", line)
        if m:
            cur = m.group(1)
            cols[cur] = []
            continue
        if cur is None:
            continue
        m = re.match(r"\s+(\w+)\s+([A-Za-z]+)(?:\([\d,]+\))?\s+.*?COMMENT\s+'([^']*)'", line)
        if m and m.group(1).upper() not in ("PRIMARY", "FOREIGN", "KEY", "UNIQUE"):
            cols[cur].append((m.group(1), m.group(2).upper(), m.group(3)))
    return cols


def family(t):
    if t in ("TINYINT",):
        return "bool"
    if t in ("INT", "BIGINT", "DECIMAL", "FLOAT", "DOUBLE", "SMALLINT"):
        return "num"
    if t in ("DATE", "DATETIME", "TIMESTAMP"):
        return "time"
    if t in ("VARCHAR", "CHAR", "TEXT"):
        return "str"
    return "?"


def cjk_ngrams(s, n=2):
    out = set()
    for seg in re.findall(r"[一-鿿]{%d,}" % n, s):
        for i in range(len(seg) - n + 1):
            w = seg[i:i + n]
            if w not in STOP:
                out.add(w)
    return out


def main():
    cols = parse_ddl()
    fks = get_foreign_keys()
    pmap = defaultdict(list)
    for fk in fks:
        pmap[fk.table].append((fk.column, fk.ref_table, fk.ref_column))

    def link(a, b):
        for ca, pa, pca in pmap.get(a, []):
            for cb, pb, pcb in pmap.get(b, []):
                if pa == pb and pca == pcb:
                    return pa, pca, ca, cb
        for ca, pa, pca in pmap.get(a, []):
            if pa == b:
                return b, pca, ca, pca
        for cb, pb, pcb in pmap.get(b, []):
            if pb == a:
                return a, pcb, pcb, cb
        return None

    def link2(a, b):
        """兩跳：A→X→P 與 B→P（或反過來）。回傳可直接 JOIN 的 (ON 子句, 說明)。"""
        for ca, pa, pca in pmap.get(a, []):
            for cx, px, pcx in pmap.get(pa, []):
                for cb, pb, pcb in pmap.get(b, []):
                    if px == pb and pcx == pcb:
                        return (f"JOIN {pa} X ON X.{pca} = A.{ca} "
                                f"JOIN {b} B ON B.{cb} = X.{cx}", f"{a}→{pa}→{px}←{b}")
        return None

    db = get_db_manager(MYSQL_URI)
    conn = db.engine.connect()

    # 只比「每個母表實體最多一列」的表，否則比不出逐列對應
    single = {}
    for t in cols:
        for c, p, pc in pmap.get(t, []):
            try:
                n = conn.execute(text(
                    f"SELECT COUNT(*) FROM (SELECT {c} FROM {t} GROUP BY {c} "
                    f"HAVING COUNT(*)>1) x")).scalar()
                single[(t, c)] = (n == 0)
            except Exception:
                single[(t, c)] = False

    names = sorted(cols)
    seen, hits, errs = set(), [], []
    for i, ta in enumerate(names):
        for tb in names[i + 1:]:
            lk = link(ta, tb)
            hop2 = None
            if lk:
                parent, ppk, fa, fb = lk
                if not (single.get((ta, fa), False) and single.get((tb, fb), False)):
                    continue
                joinsql = f"FROM {ta} A JOIN {tb} B ON A.{fa} = B.{fb}"
            else:
                # link2(a,b) 產生的 ON 子句把 a 當 A、b 當 B。反向找到時必須
                # 把別名一起換過來，否則 SQL 會參照到不存在的 A.order_id ——
                # 第一版就是這樣讓 order_profiles.installment_periods 那組整個消失。
                hop2 = link2(ta, tb)
                if hop2:
                    joinsql = f"FROM {ta} A " + hop2[0]
                else:
                    hop2 = link2(tb, ta)
                    if not hop2:
                        continue
                    joinsql = f"FROM {tb} B " + hop2[0].replace(
                        " A.", " @.").replace(f"JOIN {ta} B ", f"JOIN {ta} A ").replace(
                        " B.", " @@.").replace(" @.", " B.").replace(" @@.", " A.")
                fa = fb = None
            for ca, tya, cma in cols[ta]:
                if ca in (fa, "id", "created_at"):
                    continue
                ga = cjk_ngrams(cma)
                for cb, tyb, cmb in cols[tb]:
                    if cb in (fb, "id", "created_at"):
                        continue
                    if family(tya) != family(tyb) or family(tya) == "?":
                        continue
                    same_name = (ca == cb)
                    shared = ga & cjk_ngrams(cmb)
                    if not same_name and not shared:
                        continue
                    if hop2 and not same_name:
                        continue
                    key = (ta, ca, tb, cb)
                    if key in seen:
                        continue
                    seen.add(key)
                    co = " COLLATE utf8mb4_unicode_ci" if family(tya) == "str" else ""
                    sql = (f"SELECT COUNT(*), SUM(NOT(A.{ca}{co} <=> B.{cb}{co})) " + joinsql)
                    try:
                        n, bad = conn.execute(text(sql)).fetchone()
                    except Exception as ex:
                        errs.append((ta, ca, tb, cb, f"{type(ex).__name__}: {str(ex)[:90]}"))
                        continue
                    n, bad = int(n or 0), int(bad or 0)
                    if n == 0 or bad == 0:
                        continue
                    tag = "同欄名" if same_name else "/".join(sorted(shared)[:2])
                    if hop2:
                        tag += " ⟨2跳⟩"
                    hits.append((bad / n, bad, n, ta, ca, cma, tb, cb, cmb, tag))

    conn.close()
    hits.sort(key=lambda x: (-x[0], -x[1]))
    print(f"SQL 失敗 {len(errs)} 組 —— **失敗不等於沒事**，第一版就是這樣把")
    print("collation 錯誤當成通過，漏掉 supplier payment_terms 與 invoice_carrier。")
    for e in errs[:25]:
        print("   !!", e)
    print(f"\n值對不上的同義欄位配對 {len(hits)} 組\n")
    for r, bad, n, ta, ca, cma, tb, cb, cmb, why in hits:
        print(f"  {r*100:5.1f}%  {bad:>4d}/{n:<4d} [{why}]")
        print(f"          {ta}.{ca}  『{cma[:44]}』")
        print(f"          {tb}.{cb}  『{cmb[:44]}』")


main()
