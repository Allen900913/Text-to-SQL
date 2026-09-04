# -*- coding: utf-8 -*-
"""閘門第 [13] 項：題目形狀可判定性 —— 問句決定得了 GT 的 SELECT 清單嗎？

**為什麼閘門 [10] 抓不到（2026-09-04，ARCHITECTURE §9）**

閘門 [10]（`check_question_ambiguity.py`）問的是「答案的**來源**有沒有第二條路」：
`total_spent` 快照 vs `SUM(orders.total_amount)` 現算。它比對的是**值**。

但六輪疊出來的 8 題穩定錯裡，有 5 題的值是對的、列是對的，
**只有形狀不對**：

    #265  45 列全對，只差 GT 多一個前導 order_id
    #296  4 列全對，模型給 ticket_id、GT 要 (ticket_id, reopened_count)
    #309  其餘四欄逐位元相同，模型回「除濕機」、GT 回商品 id 11
    #287  「有沒有…？」模型回布林、GT 回 34 列明細
    #34   「哪個月比較多？各有幾張？」模型回橫向 pivot、GT 回兩列

> **閘門 [10] 檢查答案的來源，這一支檢查答案的形狀。**

**判準：問句本身有沒有決定唯一的 SELECT 清單？**

  · **有** → 模型答錯形狀就是模型的錯，題目留著，是有效的評估點。
  · **沒有** → **題目的缺陷**，要**收窄問句**。

**為什麼不是繼續補 alt_sql**。`#287` 的 note 自己列舉了明細與 COUNT 兩種，
模型六輪都給了第三種（布林 EXISTS）；`#296` 的 alt 收了 `subject`，
模型給的是 `ticket_id`。**形狀空間不是有限集合，列舉法沒有終點。**
而收窄問句是這個專案已經驗證有效的做法 —— `#261`／`#279` 就是只改問句、
沒動模型沒動資料沒加規則，兩題從 0/8 變 8/8。

**這一支只用兩種輸入**：問句字串，與 GT SQL（實際執行一次取列數）。
零 LLM、不寫任何東西進資料庫。四項檢查都是**語法級**的，沒有任何可調門檻 ——
這是刻意的，閘門 [10] 的第一版死在「用已知的那一題去調餘弦門檻」上。

    [A] 識別欄未點名   多欄投影裡有 id/no/code，而問句沒說「編號」
                       → 模型不知道該不該帶（#265 #296）
    [B] 形狀衝突的訴求 一句話同時要「哪個最大」（純量選擇）與「各有幾個」
                       （分組明細）→ 兩種形狀塞不進同一個結果集（#34）
    [C] 存在性形狀不定 「有沒有…？」而 GT 回多列多欄
                       → 布林／筆數／明細三選一（#287）
    [D] 編號或名稱     投影裡的 X_id 指向的表有 name 欄，而問句用的是實體詞
                       → 「換成的商品」是 11 還是「除濕機」（#309）
    [E] 列舉數對不上   問句「A、B、C 分別是什麼」列了 n 項，GT 投影卻是 n+k 欄
                       → GT 多帶了問句沒要的欄位（#265）

**這一支現在是偵測器，還不是閘門。** 第一次跑是要量規模 ——
在人工把名單逐題裁決完、把裁決寫進 `EXPECTED` 之前，它一律 exit 0。
裁決完之後它的職責就跟 [10]／[11] 一樣：**只守迴歸，新出現的一律紅燈。**
（[[silent-pass-is-not-a-pass]]：exit 0 的時候第一行就要說清楚為什麼是 0。）
"""
import io
import os
import re
import sys
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

# 問句說了「編號」的字樣 —— 有這些字，識別欄就是被點名的，[A]/[D] 不成立。
_ID_WORDS = ("編號", "代號", "單號", "代碼", "序號", "id", "ID", "Id", "帳號")

# 問句在問「存在嗎」的字樣。這幾個詞在中文裡同時可以答 yes/no、筆數、明細。
_EXIST_WORDS = ("有沒有", "有無", "是否", "存不存在", "是不是")

# 「把要的欄位列出來」的收尾語。[E] 只在有這種收尾語時才數得準。
_ENUM_TAIL = ("分別是什麼", "各是什麼", "分別為何", "各為何", "分別是多少",
              "各是多少", "分別有哪些", "各有哪些")

# [B]：**兩個問號不算缺陷** —— 首版這樣寫掃出 45 題，絕大多數是
# 「A、B、C 各是什麼？」那種多欄但形狀單一的題，模型答得好好的。
# 真正的病是**兩個訴求的形狀不同**：一個要純量選擇（哪個月比較多 → 1 列 1 欄），
# 一個要分組明細（各有幾張 → n 列 2 欄）。兩種塞不進同一個結果集，
# 模型只能挑一種或做橫向 pivot —— 那正是 #34 六輪都在做的事。
_PICK = ("哪個", "哪一個", "哪一", "誰", "何者", "哪家", "哪張", "哪位", "哪表")
_CMP = ("比較多", "比較少", "比較高", "比較低", "較多", "較少", "較高", "較低",
        "最多", "最少", "最高", "最低", "最大", "最小", "最長", "最短", "最先", "最晚")
_EACH = ("各有", "各是", "各為", "各多少", "各幾", "分別是", "分別為", "分別有", "各自")

# ⚠️ 不收 `*_code`。首版收了，於是把 `language_code`（評價語言）、
# `currency_code`（交易幣別）、`response_code`（回應碼）都報成識別欄 ——
# 那些是**問句自己點名要的值欄位**，不是識別欄。
_ID_COL = re.compile(r"^(id|.*_id|.*_no|.*_sn)$", re.I)

# 問句已經表明「答案是每組一列」的字樣 —— [B] 的形狀衝突不成立。
_GROUP = ("每個", "每位", "每張", "每家", "每筆", "每月", "每天", "每一",
          "各個", "各家", "各投放", "各流量", "前 ", "前3", "前5", "前 5", "前 10")


# ===========================================================================
# 靜態解析 GT SQL 的最外層投影
# ===========================================================================

def top_projection(sql: str) -> list[str] | None:
    """取最外層 SELECT 的投影清單（以逗號切開的原始字串）。

    只看括號深度 0 的 token，所以 `WITH x AS (…) SELECT a,b FROM x` 取到的是
    最後那個 SELECT、`SELECT a FROM (SELECT …) t` 取到的是第一個 —— 兩種都對。
    解析不出來回 None（**不猜**，寧可少報也不要報錯的東西）。
    """
    s = re.sub(r"\s+", " ", sql)
    depth, sel, frm = 0, -1, -1
    for m in re.finditer(r"[()]|\bSELECT\b|\bFROM\b", s, re.I):
        tok = m.group(0).upper()
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and tok == "SELECT" and sel < 0:
            sel = m.end()
        elif depth == 0 and tok == "FROM" and sel >= 0 and frm < 0:
            frm = m.start()
    if sel < 0:
        return None
    if frm < 0:
        # 沒有最外層 FROM 的純量查詢（#56 #189 的
        # `SELECT (子查詢)/(子查詢) AS pct`）—— 投影就是整段，1 欄。
        frm = len(s)
    body, depth, cur, out = s[sel:frm], 0, "", []
    for ch in body:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return [x for x in out if x and x.upper() != "DISTINCT"]


def base_col(expr: str) -> str | None:
    """投影項底下的欄位名 —— 只認「裸欄位」，有函式或運算就回 None。

    `o.order_id` → order_id；`p.name AS n` → name；
    `COUNT(*)`、`a + b`、`CASE …` → None（那些不是識別欄，[A]/[D] 不該碰）。
    """
    e = re.sub(r"\s+AS\s+\w+$", "", expr.strip(), flags=re.I)
    e = re.sub(r"\s+\w+$", "", e) if re.match(r"^[\w.`]+\s+\w+$", e) else e
    e = e.strip().strip("`")
    if not re.match(r"^[\w`]+(\.[\w`]+)?$", e):
        return None
    return e.split(".")[-1].strip("`").lower()


def group_keys(sql: str) -> int:
    """最外層 GROUP BY 有幾個運算式 —— [E] 要把分組鍵扣掉。

    「**每個月**的平均訂單金額分別是多少？」問句只列了「平均訂單金額」1 項，
    GT 投影卻是 `(月份, 金額)` 2 欄 —— 但那個月份是「每個月」隱含要的分組鍵，
    **不是 GT 多帶的欄位**。首版沒扣，#36 #49 #134 #145 #240 全被誤報。
    """
    s2 = re.sub(r"\s+", " ", sql)
    depth, g = 0, -1
    for m in re.finditer(r"[()]|GROUP\s+BY|ORDER\s+BY|HAVING|LIMIT",
                         s2, re.I):
        tok = re.sub(r"\s+", " ", m.group(0).upper())
        if tok == "(":
            depth += 1
        elif tok == ")":
            depth -= 1
        elif depth == 0 and tok == "GROUP BY" and g < 0:
            g = m.end()
        elif depth == 0 and g >= 0:
            return len([x for x in s2[g:m.start()].split(",") if x.strip()])
    if g >= 0:
        return len([x for x in s2[g:].split(",") if x.strip()])
    return 0


def enum_count(q: str) -> int | None:
    """問句列舉了幾項？沒有列舉收尾語就回 None。"""
    for tail in _ENUM_TAIL:
        if tail in q:
            head = q.split(tail)[0]
            # 只取最後一個小句 —— 前面通常是條件，不是要列舉的欄位。
            # 「？」也當小句邊界：#303 的列舉在中間那個問號之後才開始。
            head = re.split(r"[，,？?]", head)[-1] if re.search(r"[，,？?]", head) else head
            head = re.sub(r"^其|^的", "", head.strip())
            # 「跟」也是頓號的同義詞：#293「授權時間**跟**請款時間」是兩項不是一項。
            items = [x for x in re.split(r"[、,，]|與|及|和|跟", head) if x.strip()]
            return len(items) if items else None
    return None


# ===========================================================================
# 已裁決的名單 —— 人工看過一次之後才填，填之前這支不擋任何東西
# ===========================================================================
# 格式：{題號: "裁決理由"}。在名單裡 = 已經看過並判定「問句其實決定得了形狀」，
# 或「已知缺陷但決定不修」。**不在名單裡的一律要人來看一眼**（同閘門 [11]）。
#
# ---------------------------------------------------------------------------
# 2026-09-04 第一次全面裁決（20 題）—— 裁決工具是**六輪實測**，不是判斷力
# ---------------------------------------------------------------------------
# `eval_score.py` 計分時把 GT SQL 與模型 SQL 各執行一次，結果檔只存模型輸出。
# 所以 2026-09-03/04 那六輪（B A B A B A，各 309 題）可以零 API 重新計分，
# 直接回答「這個閘門標的風險，六輪裡到底發不發生」。
#
#     13 題 6/6 全過      閘門標的形狀六輪一次都沒發生
#      7 題 <6/6          再逐一看失敗長什麼樣，才知道是不是閘門標的那個形狀
#
# **關鍵發現：6/6 不等於沒有暴露。** 我一度想用「問句有沒有把主體釘成輸出
# （哪些／列出／找出）或有逐一對應詞（各自／分別／各是）」把 [A] 從 14 題收到 2 題，
# 收出來的 2 題正好是六輪最差的兩題，看起來漂亮。但拿已經確認是真缺陷的 `#265`
# 回頭測就破了 —— `#265`「訪客結帳的訂單，**其**…**分別**是什麼？」有「分別」，
# 新規則不報它。而 `#265`（0/6，真缺陷）與 `#291`（5/6，沒事）的句構幾乎一模一樣。
#
# > 所以這一支量的是**暴露**不是**缺陷**，而暴露會不會發作是擲硬幣。
# > 想把命中清單縮短的規則，都是在對這一組六輪的快照過擬合。
# > （[[stable-failures-are-usually-the-benchmark]]、§5.4）
#
# 這就是為什麼裁決要逐題寫理由、而不是改判準。
ADJUDICATED: dict[int, str] = {
    # ---- 真缺陷，已修（六輪重新計分／已重跑驗證）------------------------
    263: "[A] 真缺陷已修：問句問四件事、GT 投影五欄，三次失敗全是『23 列全對、"
         "只差前導 order_id』。2026-09-04 收窄 GT 拿掉 order_id → 六輪重算 3/6 → 6/6。"
         "修完之後改由 [C] 命中，那一個是假陽性 —— 『客人**有沒有**抱怨』是被列舉的"
         "欄位（has_complaint），跟 #270 #302 #291 同型，不是在問存在性。",
    74:  "真缺陷已修。閘門標 [D]、真正的病是 [C]：expect: empty 配存在性問法，"
         "模型回布林 [[0]] 語意正確卻被 judge() 的 empty 分支判錯。"
         "全庫 8 題 expect: empty 有 7 題用『列出／有哪些／找出』，只有這題例外 —— "
         "2026-09-04 改問句拉回同類慣例。⚠️ 改了問句，六輪對這題作廢。",
    260: "真缺陷已修。[C] 三次失敗是兩種形狀：一次布林 EXISTS、兩次回 name_en。"
         "『商品』在這個 schema 有三個名字欄。2026-09-04 改問句成『列出…的商品名稱』"
         "（字面等於 products.name 的欄位註解）並移除 COUNT alt_sql。",

    # ---- 命中屬實，但六輪的失敗**不是這個形狀**造成的（別層的病）-------
    262: "[A] 屬實但無關。5/6，唯一一次失敗是值幻覺：模型寫 channel = 'APP 行動應用'，"
         "實際值是 'APP' → 0 列。那是值層的事，不是形狀。",
    284: "[A][C] 屬實但無關。5/6，唯一一次失敗是謂詞來源誘餌：『**標**成易碎品』"
         "被讀成 shipment_profiles.has_fragile_label，正解要 JOIN 回商品標籤 → 63 vs 73 列。",
    308: "[A][C] **預測已被證實，2026-09-04 晚上收窄問句修掉**。"
         "早上的裁決寫的是「六輪失敗全是誤觸防禦暗號，病在檢索／選表層，"
         "形狀問題是潛在的，等檢索層修好之後要回頭再看一次」——"
         "當晚的欄位提示 A/B 把選表層修好了（A 臂選不到 return_profiles、B 臂選得到），"
         "潛在缺陷就照預測浮出來：B 臂不再拒答、**列數 4/4 全對**，"
         "但回的是客人姓名，因為問句主詞就是「有沒有**客人**…」而 GT 投影是退貨單四欄。"
         "**這是這支閘門第一次靠事前寫下的預測抓到東西，不是事後解釋。**",

    # ---- 暴露屬實，六輪沒發作。不修，但登記在案 -------------------------
    #      這一組全部是「GT 投影帶了一個問句沒點名的識別欄」，
    #      跟 #263／#265 同構 —— 差別只在這六輪沒擲到。
    12:  "6/6。[A] 暴露屬實但問句『訂單有哪些？是哪些客戶下的？』把訂單與客戶都要成輸出。",
    72:  "6/6。[A] 同上，『有哪些訂單…折扣金額是多少？』訂單本身就是被問的東西。",
    75:  "6/6。[A] 『找出…的訂單』，訂單是輸出。",
    104: "6/6。[A] 『列出所有付款失敗的紀錄』，紀錄是輸出。",
    282: "6/6。[A][C] 『有沒有哪些出貨…』的『哪些』已經把形狀釘成明細。",
    291: "5/6。[A][C] 與 #265 句構幾乎相同（主詞＋四項列舉＋GT 多一個 order_id），"
         "差別是這題用『各自』—— 要呈現『各自』本來就需要識別欄。"
         "唯一一次失敗是模型漏了 order_id，屬於同一個暴露、這六輪只發作一次。"
         "**不修，但它是 #263 修法的下一個候選**，再發作就照 #263 收窄。",
    300: "6/6。[A] 『有哪些訂閱扣款失敗過』，訂閱是輸出。",
    303: "6/6。[A][D][E] 『哪幾檔活動…分別是多少』，活動是輸出、列舉尾釘住形狀。",
    307: "6/6。[A][E] 『有哪些退貨…分別是什麼』，同上。",
    309: "6/6。[A] exchange_product_id 被 _ID_COL 認成識別欄，但它就是問句列舉的"
         "『換成的商品』本身。編號／名稱的歧義 2026-09-04 已補 alt_sql 收掉。",
    270: "6/6。[C] 『有沒有無障礙』是**被列舉的欄位**不是存在性問句，"
         "跟 #309 的『是否為專案通融』同型。這裡沒有『各是什麼』收尾語所以 _ENUM_TAIL "
         "擋不住 —— 這是 [C] 目前唯一的假陽性，留著不調判準（否則就是為單題調）。",
    271: "6/6。[C] 『有沒有**哪幾家**店』的『哪幾』已經把形狀釘成明細。",
    302: "6/6。[C] 『**列出**…與**是否**不可再輸入券碼』，『是否』是被列舉的欄位。",
    146: "6/6。[E] 『前 5 位客戶是誰？金額各是多少？』列舉數對不上是因為『客戶是誰』"
         "本身就是一欄，enum_count 只數到『金額』那一項。",
}


def main():
    log.remove()
    if hasattr(sys.stdout, "reconfigure"):     # Windows 主控台預設 cp950，⚠️ 會炸
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    gt = yaml.safe_load(io.open(
        os.path.join(_ROOT, "eval_ground_truth.yaml"), encoding="utf-8"))

    # X_id 指向的表有沒有 name 類欄位 —— [D] 要用。
    name_of: dict[str, bool] = {}
    rows: dict[int, int] = {}
    with get_db_manager(MYSQL_URI).engine.connect() as c:
        for t, in c.execute(text(
                "SELECT LOWER(TABLE_NAME) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE()")):
            name_of[t] = False
        for t, col in c.execute(text(
                "SELECT LOWER(TABLE_NAME), LOWER(COLUMN_NAME) "
                "FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()")):
            if col in ("name", "title", "subject", "product_name", "full_name"):
                name_of[t] = True
        for item in gt:
            if not item.get("sql"):
                continue
            try:
                rows[item["id"]] = len(c.execute(text(item["sql"])).fetchall())
            except Exception:
                rows[item["id"]] = -1

    findings: dict[int, list[str]] = {}
    unparsed = []
    for item in gt:
        qid, q, sql = item["id"], item["question"], item.get("sql")
        if not sql:
            continue                                   # absent：防禦題沒有 GT SQL
        proj = top_projection(sql)
        if proj is None:
            unparsed.append(qid)
            continue
        hits = []
        named = any(w in q for w in _ID_WORDS)
        cols = [base_col(p) for p in proj]

        # [A] 多欄投影裡有識別欄，而問句沒點名它
        if len(proj) >= 2 and not named:
            ids = [c for c in cols if c and _ID_COL.match(c)]
            if ids:
                hits.append(f"[A] 識別欄未點名：投影 {len(proj)} 欄含 {ids}，"
                            f"問句沒有「編號」字樣")

        # [B] 純量選擇 + 分組明細，兩種形狀塞不進同一個結果集
        pick = [w for w in _PICK if w in q]
        cmp_ = [w for w in _CMP if w in q]
        each = [w for w in _EACH if w in q]
        # 有分組／Top-N 標記就不算 —— #60「**每個城市**消費金額最高的客戶分別是誰？」
        # 整句都是「每組一列」，形狀是唯一的。#34 沒有這種標記：
        # 「哪個月比較多」在**全集**裡挑一個，「各有幾張」要全部列出來，才真的衝突。
        if pick and cmp_ and each and not any(g in q for g in _GROUP):
            hits.append(f"[B] 形狀衝突的訴求：「{pick[0]}…{cmp_[0]}」要純量選擇、"
                        f"「{each[0]}」要分組明細 —— 兩種形狀塞不進同一個結果集")

        # [C] 存在性問句，而 GT 回多列或多欄
        #
        # ⚠️ 有列舉收尾語就不算。#309「…補償說明與**是否**為專案通融各是什麼？」
        # 那個「是否」是**被列舉的欄位名**，不是在問存在性 —— 問句已經用
        # 「各是什麼」把形狀釘成明細了。首版沒這條，#309 被誤報成 [C]。
        if any(w in q for w in _EXIST_WORDS) and not any(t in q for t in _ENUM_TAIL):
            n = rows.get(qid, -1)
            if n > 1 or len(proj) > 1:
                hits.append(f"[C] 存在性形狀不定：「{[w for w in _EXIST_WORDS if w in q][0]}」"
                            f"而 GT 回 {n} 列 × {len(proj)} 欄 → 布林／筆數／明細三選一")

        # [D] X_id 指向的表有 name 欄，而問句用的是實體詞
        if not named:
            for c0 in cols:
                if c0 and c0.endswith("_id"):
                    for cand in (c0[:-3] + "s", c0[:-3]):
                        if name_of.get(cand):
                            hits.append(f"[D] 編號或名稱：{c0} → `{cand}` 有 name 欄，"
                                        f"問句用實體詞而非「編號」")
                            break
                    else:
                        continue
                    break

        # [E] 列舉數與投影欄數對不上
        n_enum = enum_count(q)
        # 分組鍵最多只扣到「剩一欄」為止 —— #49 的 `GROUP BY c.id, c.name` 有兩個鍵，
        # 但只有 `c.name` 真的被投影出來，全扣會扣過頭。
        n_key = min(group_keys(sql), len(proj) - 1)
        if n_enum is not None and len(proj) - n_key != n_enum:
            hits.append(f"[E] 列舉數對不上：問句列了 {n_enum} 項，"
                        f"GT 投影 {len(proj)} 欄"
                        + (f"（扣掉 {n_key} 個分組鍵仍多 "
                           f"{len(proj) - n_key - n_enum}）" if n_key else ""))

        if hits:
            findings[qid] = hits

    # ------------------------------------------------------------------
    stable8 = {34, 143, 265, 287, 292, 296, 308, 309}
    tags = Counter(h[:3] for hs in findings.values() for h in hs)

    print(f"閘門 [13] 題目形狀可判定性 —— 掃過 {len(gt)} 題"
          f"（{len([g for g in gt if g.get('sql')])} 題有 GT SQL）\n")
    for qid in sorted(findings):
        mark = " ★穩定錯" if qid in stable8 else ""
        alt = f"（有 alt_sql×{len(gt[qid - 1].get('alt_sql') or [])}）" \
            if next(g for g in gt if g["id"] == qid).get("alt_sql") else ""
        q = next(g for g in gt if g["id"] == qid)["question"]
        print(f"#{qid:<4d}{mark}{alt} {q}")
        for h in findings[qid]:
            print(f"        {h}")
    print("-" * 78)
    print(f"命中 {len(findings)} / {len(rows)} 題　"
          + "　".join(f"{k} {v}" for k, v in sorted(tags.items())))
    print(f"八題穩定錯裡掃到：{sorted(findings.keys() & stable8)}　"
          f"漏掉：{sorted(stable8 - findings.keys())}")
    if unparsed:
        print(f"⚠️ 解析不出最外層投影 {len(unparsed)} 題：{unparsed}")

    new = set(findings) - set(ADJUDICATED)

    # 陳舊的裁決鍵 —— 題目改掉之後裁決還留著，就是一個**會靜默放行的洞**：
    # 有人把問句改回去，這支不會再報。所以每次都把它列出來，
    # 而且要說清楚「留著是文件，不是通過」（[[silent-pass-is-not-a-pass]]）。
    stale = sorted(set(ADJUDICATED) - set(findings))
    if stale:
        print(f"\nℹ️ 已裁決但現在不再命中的 {len(stale)} 題：{stale}")
        print("   ——問句／GT 已經改掉了。理由留在 ADJUDICATED 當紀錄，"
              "但**它們現在不受這支保護**：改回去不會再報。")

    if not ADJUDICATED:
        print(f"\n⚠️ exit 0 **不代表通過** —— `ADJUDICATED` 還是空的，"
              f"這支現在是偵測器不是閘門。\n"
              f"   上面 {len(findings)} 題要人工逐題裁決（問句決定得了形狀嗎？），"
              f"裁決結果寫進 ADJUDICATED 之後才會開始擋。")
        sys.exit(0)
    if new:
        print(f"\n!! 未裁決的題目 {len(new)} 題：{sorted(new)} —— 逐題判：\n"
              f"   問句決定得了 GT 的 SELECT 清單嗎？決定得了就寫進 ADJUDICATED，"
              f"決定不了就收窄問句。")
        sys.exit(1)
    print(f"\n閘門 [13] 綠燈：{len(findings)} 題命中形狀檢查，全部已裁決。")


if __name__ == "__main__":
    main()
