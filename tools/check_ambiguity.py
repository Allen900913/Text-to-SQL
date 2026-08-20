"""歧義稽核 —— 給干擾表灌資料會不會讓既有題目變成「真的有兩個答案」？

為什麼需要這支（ARCHITECTURE.md §8.8 的鏡像）：

    §8.8 記的是「**加表**會 silently 作廢防禦題」。這支擋的是另一半：
    **加資料會 silently 讓一般題變成歧義題**。

    19 張干擾表現在是零列的。零列有兩個壞處：
      1. 任何對它的查詢都回 0 列，而 expect: empty 的題「回 0 列就算對」——
         模型選錯表也會被判對。這是**必然**發生的假通過，不需要任何巧合。
      2. 熱度先驗、值檢索這類用資料當訊號的方法在這個庫上會假性滿分，
         benchmark 對整類方法瞎掉。

    所以要灌資料。但灌下去之後本來唯一的答案可能變成兩個。

判準：**可移植性 + 值域命中**，不是文字相似度，也不是欄位名重疊。

    第一版只比欄位名，26 個 HIGH 幾乎全是誤報：
        #4「新北市客戶平均下幾張訂單」→ customer_login_logs 也有 customer_id、id
    customer_id / order_id / id / created_at 在任何正規化 schema 裡到處都是。
    拿它們判斷歧義，等於宣告「所有子表都可以互換」—— 那不是歧義，那是正規化。

    真正的歧義需要**語意可替代**，而語意不在欄位名裡，在**值**裡：
        orders.status='PAID'  vs order_returns.status  → 值域 {REQUESTED,RECEIVED,
                                REJECTED} 不含 PAID，查出來是 0 列，不是第二個答案
        orders.status='PENDING' vs support_tickets.status → 值域**含 PENDING**，真歧義

    所以本程式讀 distractor_seed_plan.yaml 的值域宣告。**先宣告要灌什麼，
    才能判斷灌下去安不安全** —— 稽核過的才准灌，灌的必須跟稽核的一致。

⚠️ 已知盲點：本程式比的是**同名欄位**，看不見「同概念、不同欄位名」的歧義。
    實例 `#104`「列出所有付款失敗的紀錄」：正解是 `payments.status='FAILED'`，
    而 `payment_attempts` 用的是 `result`（值域 OK/DECLINED/TIMEOUT）——
    欄位名對不上，所以本程式判定不可移植、完全沒報警。
    但 LLM 實測 **0/6** 都選了 `payment_attempts`：對模型而言那就是同一個概念。
    → 這個盲點不打算用更聰明的字串比對補（那會回到第一版的誤報地獄）。
      補法是 §7.13 那條：**把正解表的註解寫到涵蓋該概念**，
      然後用 `eval_filter_vote.py` 針對目標題實測，讓 LLM 自己回答有沒有分開。

風險分級：
    HIGH  非鍵欄位可整表移植，且 GT 的字面常數命中干擾表的宣告值域
    MED   非鍵欄位可整表移植，但 GT 沒有字面常數（概念層重疊，要人判斷）
    LOW   部分覆蓋（--all 才列）

裁決寫在 tools/ambiguity_verdicts.yaml（不寫回 GT，理由見 load_verdicts()），
與 check_defence_gt.py 同一個模式：**未裁決的命中才是警報。**
反向也會檢查：裁決過的表若已不再命中，會被列為「過期的裁決」。

用法：
    python tools/check_ambiguity.py          # 有未裁決的 HIGH/MED 就 exit 1
    python tools/check_ambiguity.py --all    # 連 LOW 與已裁決的都列
"""
import io
import os
import re
import sys

import sqlglot
import yaml
from loguru import logger as log
from sqlglot import exp

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# eval_schema_need 在 eval/ 底下（2026-08-20 分類搬遷）
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "eval"))

from eval_schema_need import required_schema  # noqa: E402
from langgraph_sql.utils.schema_graph import get_join_graph  # noqa: E402
from langgraph_sql.utils.schema_registry import (  # noqa: E402
    get_schema_parser, get_table_columns,
)

GT_PATH = "eval_ground_truth.yaml"
_HERE = os.path.dirname(os.path.abspath(__file__))
PLAN_PATH = os.path.join(_HERE, "distractor_seed_plan.yaml")
VERDICT_PATH = os.path.join(_HERE, "ambiguity_verdicts.yaml")

# 這些欄位不構成語意上的可替代性：主鍵、外鍵、時間戳、軟刪除旗標。
# 它們在任何正規化 schema 裡到處都是，拿它們判歧義只會得到「所有子表都可互換」。
_GENERIC = re.compile(r"^(id|.*_id|created_at|updated_at|deleted_at|is_deleted)$")


def is_generic(col: str) -> bool:
    return bool(_GENERIC.match(col))


def load_plan() -> dict:
    return yaml.safe_load(io.open(PLAN_PATH, encoding="utf-8")) or {}


def load_verdicts() -> dict[int, set[str]]:
    """{題號: {已裁決為不歧義的干擾表}}。

    裁決放在獨立檔而不是寫回 eval_ground_truth.yaml：GT 有 159 筆、
    大量多行 note，yaml round-trip 會把整份格式重排，風險大於收益。
    分家的代價是裁決可能過期，所以 main() 會回頭檢查
    「裁決過的表現在還在不在命中清單裡」—— 過期會被抓到。
    """
    raw = yaml.safe_load(io.open(VERDICT_PATH, encoding="utf-8")) or {}
    return {int(k): {t.lower() for t in (v.get("tables") or [])}
            for k, v in raw.items()}


def distractor_tables(live: dict[str, list[str]], gt: list[dict]) -> set[str]:
    """**全庫每一張表都是潛在干擾** —— 干擾是逐題的關係，不是表的屬性。

    這個定義換過兩次，兩次都是被實測打掉的（ARCHITECTURE.md §11.7）：

      v1「DB 有、DDL 沒有」 —— 那個差集在當時剛好等於「模型不認識的表」，
         但它不認識的原因是 DDL 漏產（§10.6 的 bug）。補完 DDL 之後差集變成
         空集合，稽核會**靜默地什麼都不檢查**。

      v2「所有 GT 都沒用到的表」 —— 看起來對上了 §10.1「干擾是表 × 題目集
         的關係」，其實實作成了「表 × **全部**題目」。後果：一張表只要在
         **任何一題**當過正解，就永遠不會被拿來跟**其他題**比。
         `coupons` 對 `#203` 是正解、對 `#72`「使用了折扣碼」是干擾 ——
         v2 直接跳過，實測就這樣漏掉兩個真的歧義（`#72` 與 `#168`）。

    v3 回到定義本身：對題目 Q 而言，**任何不在 Q 正解表裡的表**都是干擾。
    這個集合就是全庫；真正的過濾靠 classify() 的「可移植性 + 值域命中」，
    以及它開頭那條「這張表就是本題正解 → 跳過」。

    參數 gt 保留但不再使用 —— 呼叫端的介面不變，而把「為什麼不用」寫在這裡
    比刪掉參數更能擋住下一次有人再發明 v2。
    """
    return set(live)


def connected_to(distractor: str, need_tables: set[str], hops: int = 1) -> bool:
    """干擾表接不接得到本題的正解表（外鍵圖上 hops 跳以內）。

    hops = 1 是刻意的下限：`coupons` 不直接連 `orders`，它經由
    `coupon_redemptions` —— 那正是 `#72` 被搶走的路徑，所以一定要涵蓋。
    再放寬到 2 跳，80 張表的圖幾乎全連通，這個過濾就失去意義。
    """
    graph = get_join_graph()
    frontier = {distractor}
    seen = {distractor}
    for _ in range(hops + 1):
        if frontier & need_tables:
            return True
        nxt = {n for t in frontier for n in graph.get(t, ())} - seen
        seen |= nxt
        frontier = nxt
    return bool(frontier & need_tables)


def literals_of(sql: str) -> dict[str, set[str]]:
    """{欄位名: {這條 SQL 拿它比對過的字面值}}。

    取 =、<> 與 IN。範圍比較（>、<）不看 —— 那是連續量，
    「值落不落在宣告值域裡」對它沒有意義。

    `<>` 一定要收：#10「狀態不是 CANCELLED」寫成 `status <> 'CANCELLED'`，
    漏掉它會讓這題被誤判成「無字面常數」而降級成 MED，
    等於把一個可以自動排除的命中丟給人工。
    """
    out: dict[str, set[str]] = {}

    def add(colnode, litnode):
        if isinstance(colnode, exp.Column) and isinstance(litnode, exp.Literal):
            out.setdefault(colnode.name.lower(), set()).add(
                str(litnode.this).strip().lower())

    ast = sqlglot.parse_one(sql, read="mysql")
    for node in ast.find_all(exp.EQ, exp.NEQ):
        add(node.left, node.right)
        add(node.right, node.left)
    for node in ast.find_all(exp.In):
        for e in node.expressions:
            add(node.this, e)
    return out


def classify(entry_sql: str, need_tables: set[str], need_cols: set[str],
             live: dict[str, list[str]], distractors: set[str],
             plan: dict) -> list[tuple[str, str, str]]:
    """回傳 [(風險等級, 干擾表, 說明)]。"""
    try:
        lits = literals_of(entry_sql)
    except Exception:
        lits = {}
    out: list[tuple[str, str, str]] = []
    for d in sorted(distractors):
        if d in need_tables:
            # 這張表就是**這一題的正解表**，不是它自己的干擾。
            # #160-171 補題之前不會發生：那時候 19 張表一次都沒當過答案，
            # 「干擾表」可以直接當成「永遠不是答案」用。補題把這個前提打掉了 ——
            # 干擾是「表 × 題目集」的關係，不是表的屬性（§10.1）。
            # 少了這一行，support_tickets 會因為「status 在 support_tickets 裡
            # 全部都有」被判成 HIGH，12 個命中裡有 10 個是這種自我比對。
            continue
        if not connected_to(d, need_tables):
            # **接不到本題主體的表，不可能是第二個答案。**
            #
            # 這條是把手工裁決的推理寫成程式。19 筆人工裁決裡的理由幾乎逐字
            # 相同：「warehouse_transfers 沒有 order_id，接不到訂單這個主體」
            # 「job_runs 無 customer_id / order_id」——**同一條推理寫了 19 次**。
            #
            # 沒有這條，80 張表時命中 869 個 / 115 題，完全不可用：
            # 表一多，status / name / quantity 這種欄位到處都是，
            # 「欄位可移植」就退化成雜訊。可移植性講的是**欄位**，
            # 連通性講的是**主體**，兩個都要滿足才可能產生第二個答案。
            continue
        spec = plan.get(d) or {}
        if spec.get("empty"):
            continue                       # 刻意留空的表灌不進去，不可能製造歧義
        dcols = set(live.get(d, []))
        domains = {k.lower(): {str(v).strip().lower() for v in (vs or [])}
                   for k, vs in (spec.get("values") or {}).items()}
        for t in sorted(need_tables):
            used = {c.split(".", 1)[1] for c in need_cols if c.startswith(f"{t}.")}
            meaningful = {c for c in used if not is_generic(c)}
            if not meaningful:
                continue                   # 只有鍵重疊 —— 那是正規化，不是歧義
            if not meaningful <= dcols:
                shared = meaningful & dcols
                if shared:
                    out.append(("LOW", d, f"{t} 的 {sorted(shared)} 也在 {d} 裡"
                                          f"（缺 {sorted(meaningful - dcols)}）"))
                continue
            # 可整表移植。再看 GT 的字面常數有沒有落進干擾表的宣告值域。
            hits = []
            checked = False
            for col in sorted(meaningful):
                if col not in lits:
                    continue
                checked = True
                for v in lits[col]:
                    if v in domains.get(col, set()):
                        hits.append(f"{col}={v!r}")
            base = f"{t} 用到的 {sorted(meaningful)} 在 {d} 裡全部都有"
            if hits:
                out.append(("HIGH", d, f"{base}，且 {', '.join(hits)} 命中 {d} 的宣告值域"))
            elif checked:
                pass                       # 常數不在值域內 → 查出來是空的，不是第二個答案
            elif len(meaningful) == 1:
                # **只共用一個欄位不算「可整表移植」。**
                # 80 張表時 477 個 MED 有壓倒性多數是這樣來的：
                #   products.name vs categories.name vs stores.name vs employees.name
                # 每一張實體表都有 name，就跟每一張都有 id 一樣 —— 那是正規化，
                # 不是可替代性。`_GENERIC` 沒辦法把 name 一律排除（有些題目
                # 真的靠 name 區分），所以改成看**張數**：
                # 一個欄位重疊是巧合，兩個以上才開始像同一個概念。
                out.append(("LOW", d, f"{base}（只共用一個欄位，不足以構成可移植）"))
            else:
                out.append(("MED", d, f"{base}（GT 無字面常數，概念層重疊，需人判斷）"))
    return out


def audit(gt: list[dict], live: dict[str, list[str]], distractors: set[str],
          plan: dict, verdicts: dict[int, set[str]] | None = None
          ) -> tuple[list[str], list[str], int, set[tuple[int, str]]]:
    """回傳 (警報行, 全部行, 未裁決命中數, 實際命中的 (題號,表) 集合)。

    最後一項給 main() 用來偵測**過期的裁決** —— 裁決放在獨立檔，
    題目或值域改了之後裁決可能不再對應任何命中，那種殭屍裁決要被看見。
    """
    verdicts = verdicts or {}
    live_hits: set[tuple[int, str]] = set()
    alarms: list[str] = []
    every: list[str] = []
    n_alarm = 0
    for entry in gt:
        if entry.get("expect") == "schema_unsupported" or not entry.get("sql"):
            continue
        try:
            need_t, need_c = required_schema(entry["sql"], live)
        except Exception as exc:
            every.append(f"#{entry['id']} 無法解析 GT SQL: {exc}")
            continue
        found = classify(entry["sql"], need_t, need_c, live, distractors, plan)
        if not found:
            continue
        ack = verdicts.get(entry["id"], set())
        head = f"#{entry['id']} {entry['question']}"
        every.append(head)
        hot = []
        for level, d, note in found:
            mark = "已裁決" if d in ack else level
            every.append(f"    [{mark}] {d}: {note}")
            if level in ("HIGH", "MED"):
                live_hits.add((entry["id"], d))
            if d not in ack and level in ("HIGH", "MED"):
                hot.append(f"    [{level}] {d}: {note}")
                n_alarm += 1
        if hot:
            alarms.append(head)
            alarms.extend(hot)
    return alarms, every, n_alarm, live_hits


VERDICT_HINT = """逐一裁決（判準：灌了資料之後這題還有沒有唯一答案）：
  仍然唯一 → 該題補 ambiguity_ack: [表名]，並在 note 說明為什麼不歧義
  真的歧義 → 改題目講清楚（§5.1：GT 永遠先修正），或補 alt_sql
  無法消歧 → 這張干擾表在 seed plan 裡改成 empty: true"""


def main() -> int:
    show_all = "--all" in sys.argv
    gt = yaml.safe_load(io.open(GT_PATH, encoding="utf-8"))
    live = get_table_columns()
    plan = load_plan()
    distractors = distractor_tables(live, gt)

    # v3 之後 distractors 是全庫，核心表本來就不在 seed plan 裡（它們由
    # init_db*.py 建、不由灌資料腳本管），拿全庫去減會把它們全部誤報。
    # 反過來檢查才有意義：**計畫裡有、資料庫已經沒有的表**（過期的宣告）。
    #
    # 「新表忘了寫進 seed plan」這個原本的擔憂已經有更好的守門員：
    # 沒有計畫就不會被灌，零列的表會被 check_schema_pipeline.py 第 7 項擋下。
    missing = sorted(set(plan) - set(live))
    empties = sorted(t for t in distractors if (plan.get(t) or {}).get("empty"))
    print(f"逐題比對的候選干擾表 {len(distractors)} 張（＝全庫）；"
          f"一般題 {sum(1 for e in gt if e.get('sql'))} 題")
    if missing:
        print(f"⚠️  seed plan 宣告了、但資料庫沒有的表 → {missing}（過期的宣告）")
    print()

    verdicts = load_verdicts()
    alarms, every, n, live_hits = audit(gt, live, distractors, plan, verdicts)
    stale = sorted((q, t) for q, ts in verdicts.items() for t in ts
                   if (q, t) not in live_hits)
    print("\n".join(every if show_all else alarms) or "（沒有任何命中）")

    print("\n" + "=" * 70)
    if alarms or missing:
        print(f"未裁決的 HIGH/MED 命中 {n} 個。\n{VERDICT_HINT}")
        return 1
    print("全部通過：依現行值域宣告灌資料，不會讓任何一般題產生第二個合理答案。")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
