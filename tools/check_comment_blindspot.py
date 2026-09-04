"""表註解盲區偵測 —— 下一個 `#104` / `#154` 在哪裡？

為什麼需要這支（ARCHITECTURE.md §6.4）：

    `#104` 與 `#154` 是同一個病，而且結構逐字相同：

        概念寫在**欄位註解**裡（payments.status「SUCCESS/FAILED/REFUNDED」、
        customer_profiles.email_verified_at「Email 驗證通過時間」）
        但**表註解**沒有它 —— 而候選清單只給表註解（§2.8）。
        更糟的是**競爭表的表註解裡有那個關鍵詞**
        （payment_attempts「含失敗的」、customers「記錄姓名、Email、電話」）。

    於是問句的關鍵詞字面命中錯的那張表，對的那張表在目錄裡是隱形的。
    兩題都是靠補正解表的表註解修好的（`#104` 0/6→6/6、`#154` 4/8→8/8，p=0.038）。

    這支把那個診斷自動化：**在被咬到之前找出下一個。**

判準：**問句驅動**，不是表驅動。

    第一版是表驅動的（對每張表列出它欄位註解有、表註解沒有的詞），
    40 個候選裡大部分是誤報，而且是設計缺陷不是調參問題：

        [誘餌] customer_profiles 缺概念詞「瀏覽」  競爭表 → ['browse_logs']

    `browse_logs` 對瀏覽題**本來就是正解表**。表驅動的版本不知道哪張表才對，
    所以對每個 (表, 缺的詞) 都標誘餌，方向可能整個反過來。

    而 GT SQL 就是「哪張表才對」的答案。所以改成：

    對每一題 Q（正解表集合 N，由 GT SQL 解出）
      對 Q 裡的每個概念詞 c
        對每張正解表 T ∈ N
          c ∈ T 的**欄位**註解  且  c ∉ T 的**表**註解        → T 在目錄裡被這個詞找不到
          且 c ∈ 某張**非正解**表 U 的表註解                    → **誘餌**：U 會贏走它（HIGH）
          否則                                                → **盲區**：沒有表能被它找到（MED）

    這正是 `#104` / `#154` 的結構，而且不會把正解表誤標成競爭者。

⚠️ 這支只給候選，不給處方。§2.10 稀釋定律說不能把每張表註解都養胖 ——
   修之前照 §6.4 的六步走，尤其是第 4 步（補概念家族，不補那一題的關鍵詞）
   與第 5 步（新舊註解頭對頭的稀釋鏡像檢查）。

用法：
    python tools/check_comment_blindspot.py
    python tools/check_comment_blindspot.py --table customer_profiles
"""
import io
import os
import re
import sys
from collections import defaultdict

import yaml
from loguru import logger as log
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# eval_schema_need 在 eval/ 底下（2026-08-20 分類搬遷）
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval"))

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

GT_PATH = "eval_ground_truth.yaml"

# 概念詞：2~6 個中文字，或 3 個字以上的英文詞。
# 中文沒有空格，所以只能用 n-gram —— 這會產生大量重疊的碎片，
# 後面用「有沒有 GT 問句用到」把它們篩掉，比裝一個斷詞器便宜也更準。
_CJK = re.compile(r"[一-鿿]{2,6}")
_EN = re.compile(r"[A-Za-z][A-Za-z_]{2,}")

# 這些詞出現在幾乎每張表的欄位註解裡，不帶區辨力
_STOP = {
    "唯一", "時間", "紀錄", "資料", "編號", "名稱", "數量", "金額", "狀態",
    "是否", "對應", "所屬", "建立", "更新", "版本", "業務", "欄位", "這張",
    "可能", "不同", "表示", "使用", "例如", "非業務欄位", "唯一ID",
    "created", "updated", "int", "varchar", "datetime", "decimal", "null",
}


def load_meta():
    """(表註解, 每張表的欄位註解串接)。一律以 INFORMATION_SCHEMA 為準。"""
    with get_db_manager(MYSQL_URI).engine.connect() as conn:
        tbl = {t.lower(): (c or "") for t, c in conn.execute(text("""
            SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES
            WHERE TABLE_SCHEMA = DATABASE()"""))}
        cols: dict[str, list[str]] = defaultdict(list)
        for t, c, cc in conn.execute(text("""
            SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT
            FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()
            ORDER BY TABLE_NAME, ORDINAL_POSITION""")):
            cols[t.lower()].append(f"{c} {cc or ''}")
    return tbl, {k: " ".join(v) for k, v in cols.items()}


def terms_of(blob: str) -> set[str]:
    """問句裡所有 2~6 字的中文子字串（真的滑動視窗）＋ 3 字以上的英文詞。

    ⚠️ 2026-09-04 修正一個從第一版就在的缺陷。原本是 `_CJK.findall(...)`，
    而 `findall` 對 `[一-鿿]{2,6}` 取的是**貪婪、不重疊的定長切塊**，
    不是 n-gram —— 一個概念抓不抓得到，取決於它在問句裡的**位元組位置**：

        「有哪些付款到現在還沒跟對帳單勾稽完成？」
            → ['有哪些付款到', '現在還沒跟對', '帳單勾稽完成']

    「對帳單勾稽」被切在兩塊中間，兩塊都不存在於任何註解，於是 `#292` 靜默漏掉。
    這也解釋了為什麼舊輸出裡的概念詞長得那麼怪 ——「戶姓名」「易幣別」「冊時間」
    「的商品」「有人」全都是切塊的殘骸，不是概念。

    改成真的滑動視窗之後碎片會變多，但**後面兩道過濾本來就是為此設計的**
    （必須出現在正解表的欄位註解裡、且不在表註解裡），再加上 audit() 只留極大詞。
    """
    out = set(_EN.findall(blob))
    for run in re.findall(r"[一-鿿]+", blob):
        for n in range(2, 7):
            for i in range(len(run) - n + 1):
                out.add(run[i:i + n])
    return {t for t in out if t.lower() not in {s.lower() for s in _STOP}}


def gt_cases():
    """[(題號, 問句, 正解表)]。防禦題沒有 GT SQL，排除。"""
    from eval_schema_need import required_schema
    from langgraph_sql.utils.schema_registry import get_table_columns
    known = get_table_columns()
    out = []
    for e in yaml.safe_load(io.open(GT_PATH, encoding="utf-8")):
        if e.get("expect") == "schema_unsupported" or not e.get("sql"):
            continue
        try:
            need, _ = required_schema(e["sql"], known)
        except Exception:
            continue
        if need:
            out.append((e["id"], e["question"], need))
    return out


def audit(tbl: dict[str, str], colblob: dict[str, str], cases) -> list[tuple]:
    """回傳 [(等級, 題號, 正解表, 概念詞, 會贏走它的非正解表)]。

    概念詞從**問句**抽（問句短，n-gram 不會爆炸），
    再要求它出現在正解表的欄位註解裡 —— 那一步同時濾掉了碎片詞：
    「款方式」不會出現在任何欄位註解裡，「Email 驗證」會。
    """
    # 一個詞住在幾張表的欄位註解裡。**跨太多張就沒有鑑別力** ——
    # 「完成」「的付款」「不符合」在幾十張表的欄位註解裡都有，報出來只是噪音。
    # 上限沿用 value_index.VALUE_MAX_TABLES 的 2，不新開一個旋鈕
    # （[[discriminative-not-just-nonempty]]、§10「停止調常數」）。
    from langgraph_sql.utils.value_index import VALUE_MAX_TABLES
    spread: dict[str, int] = {}

    def discriminative(c: str) -> bool:
        if c not in spread:
            lc = c.lower()
            spread[c] = sum(1 for b in colblob.values() if lc in b.lower())
        return spread[c] <= VALUE_MAX_TABLES

    findings = []
    for qid, q, need in cases:
        for c in sorted(terms_of(q)):
            if not discriminative(c):
                continue
            for t in sorted(need):
                if c.lower() not in colblob.get(t, "").lower():
                    continue                     # 這張表的欄位沒這個概念
                if c.lower() in tbl.get(t, "").lower():
                    continue                     # 表註解已經有了
                rivals = sorted(u for u, uc in tbl.items()
                                if u not in need and c.lower() in uc.lower())
                findings.append(("HIGH" if rivals else "MED", qid, t, c, rivals))

    # 真滑動視窗會讓同一個概念以「對帳單」「帳單勾」「對帳單勾稽」…重複出現。
    # 同一個 (題, 表) 底下只留**極大詞** —— 被更長的命中詞包住的一律丟掉。
    # 這與 value_index.matches() 的最長匹配優先是同一條紀律。
    by_case: dict[tuple, list[tuple]] = defaultdict(list)
    for f in findings:
        by_case[(f[1], f[2])].append(f)
    maximal = []
    for group in by_case.values():
        words = {g[3] for g in group}
        maximal += [g for g in group
                    if not any(w != g[3] and g[3] in w for w in words)]
    return sorted(maximal, key=lambda f: (f[0] != "HIGH", -len(f[4]), f[1]))


def main() -> int:
    only = None
    if "--table" in sys.argv:
        only = sys.argv[sys.argv.index("--table") + 1]

    tbl, colblob = load_meta()
    cases = gt_cases()
    findings = audit(tbl, colblob, cases)
    if only:
        findings = [f for f in findings if f[2] == only]

    qtext = {qid: q for qid, q, _ in cases}
    hi = [f for f in findings if f[0] == "HIGH"]
    print(f"全庫 {len(tbl)} 張表 / {len(cases)} 題；"
          f"誘餌 {len(hi)} 個、盲區 {len(findings) - len(hi)} 個\n")

    # 逐表彙總先印 —— 只印前 40 筆時，**最重要的兩筆可能正好在摺線下面**。
    # 2026-09-04 的實測就是這樣：修好 n-gram 之後 #292 與 #308 終於被抓到，
    # 但全庫 302 筆排下來它們在 40 名之外，`grep '#292'` 一無所獲
    # （[[silent-pass-is-not-a-pass]]：被截斷的輸出跟沒報一樣）。
    per_table: dict[str, list] = defaultdict(list)
    for f in findings:
        per_table[f[2]].append(f)
    print(f"{'表':26s} {'誘餌':>4s} {'盲區':>4s}  題號")
    for t in sorted(per_table, key=lambda x: -len(per_table[x])):
        g = per_table[t]
        n_hi = sum(1 for f in g if f[0] == "HIGH")
        qs = sorted({f[1] for f in g})
        print(f"  {t:24s} {n_hi:>4d} {len(g) - n_hi:>4d}  "
              f"{','.join(f'#{q}' for q in qs[:8])}"
              f"{f' …+{len(qs) - 8}' if len(qs) > 8 else ''}")
    print()

    shown = findings if only else findings[:40]
    for level, qid, t, c, rivals in shown:
        tag = "誘餌" if level == "HIGH" else "盲區"
        print(f"[{tag}] #{qid} 需要 {t}，但它的表註解沒有「{c}」")
        print(f"        問句 → {qtext[qid][:44]}")
        if rivals:
            print(f"        這些非正解表的註解裡有「{c}」→ {rivals[:5]}")
    if len(shown) < len(findings):
        print(f"\n⚠️ 只印了前 {len(shown)} 筆，另有 {len(findings) - len(shown)} 筆沒印出來 ——\n"
              f"   **不要用 grep 在這份輸出上找某一題**，會得到假的「沒問題」。\n"
              f"   要看某一張表的全部：--table <表名>（指定表時不截斷）。")

    print("\n" + "=" * 70)
    print("這是候選不是處方。修之前照 §6.4 六步走 ——\n"
          "  補概念家族不補關鍵詞；改 init_db*.py 唯一來源；\n"
          "  新舊註解頭對頭跑稀釋鏡像檢查（§2.10 說不能把每張表都養胖）；\n"
          "  最後 eval_stability.py 驗目標題 + 同表其他題無回歸。")
    return 1 if hi else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
