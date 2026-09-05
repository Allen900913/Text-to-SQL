"""
從 INFORMATION_SCHEMA 註解 ＋ 真實 DISTINCT 值產生 enum_fields
==============================================================
ARCHITECTURE.md §9.12：48 個代碼欄位的合法值，只以「CODE 中文」的散文形式
活在 COLUMN_COMMENT 裡。模型會把「APP 行動應用」整串讀成一個值，寫出
`channel = 'APP 行動應用'`（實際值是 'APP'）。同一種錯在兩次不同實驗的
不同臂各出現過一次。

這是 [[schema-source-must-be-wired]] 的另一個形狀：兩次擴表時 DDL 接上了
gen_ddl.py，enum_fields 沒有人接，所以現有 6 個 enum 全在原本的窄表，
與這 48 個零交集。

**值取自資料，不取自註解文字**（[[comment-examples-from-data-not-questions]]）：

    值域   SELECT DISTINCT          ← 唯一權威。註解會寫錯、會過期。
    中文   COLUMN_COMMENT 解析       ← 只是給模型對照「中文問句 → 代碼」用。
                                       解析不出來就留空，欄位照樣列出來。

兩種落差**只報告、不擅自修**（報給人看，因為它們的成因不同）：
  ① 註解宣告了、資料 0 筆   → 死代碼，或種資料時漏了這一類
  ② 資料有、註解沒提        → 模型完全沒機會知道這個值存在

這是 **prompt 層介入**，照 §9.10 要事前登記再量，而且不可以直接翻預設值。
所以有 `--out`：成品寫到另一個檔，兩臂在同一個 commit 上跑。

    python tools/gen_enum_fields.py                                       # 預覽 ＋ 落差報告
    python tools/gen_enum_fields.py --out utils/semantic_layer_enum.yaml  # 產 B 臂
    SEMANTIC_LAYER_PATH=utils/semantic_layer_enum.yaml python eval/test_runner.py
    python tools/gen_enum_fields.py --write                               # 量完才翻預設
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yaml  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402

YAML_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "utils", "semantic_layer.yaml",
)

BEGIN = "  # >>> gen_enum_fields.py 產生，勿手動編輯（改註解或資料後重跑）"
END = "  # <<< gen_enum_fields.py 產生結束"

# 值域大於這個數就不列 —— enum 的用途是「窮舉一個小的封閉集合」。
# 值太多代表它其實是自由文字或識別碼，列出來只是灌 Prompt。
MAX_VALUES = 12

# 只看短字串欄。代碼是短的；自由文字與識別碼不是。
# 這是型別判斷，不是欄名的啟發式規則 —— 用欄名猜「哪些像代碼欄」
# 會變成另一組要維護的規則（[[few-shot-not-rules]] 的同一個毛病）。
MAX_LEN = 40

# 註解裡的分隔符。`、` 一定要在裡面：payment_attempts.result 寫的是
# 「DECLINED 銀行拒絕、TIMEOUT 逾時未回應」，漏掉 `、` 會把 TIMEOUT
# 吃進上一個代碼的中文裡。括號也要，原窄表的風格是 `動作 (INSERT/UPDATE/DELETE)`。
_SEPS = r"／/、；;，,()（）：:"
_SEP_RE = re.compile(f"[{re.escape(_SEPS)}]")

# 這個庫有兩種註解風格，**parser 不能只認得其中一種**：
#   A（原窄表）    動作 (INSERT/UPDATE/DELETE)          括號、斜線、沒有中文
#   B（*_profiles） 出價策略：CPC 單次點擊／CPM 千次曝光   全形／、代碼後接中文
# 所以下面不切段、也不猜哪個 token 是代碼 —— **拿資料裡的真值去註解裡找**。
_NUMERIC = re.compile(r"^[\d.\-]+$")

# 詞邊界**必須含連字號**。少了它，`評價語言：zh-TW／en／ja` 會被挖出一個
# `TW`，於是 review_profiles.language_code 被報成「宣告了 TW、資料 0 筆」——
# 那是這支自己造出來的死代碼，不是資料的問題。三處（找代碼、找真值、取中文）
# 用同一個邊界，不要各寫各的。
_B = "A-Za-z0-9_-"
_CODEISH = re.compile(f"(?<![{_B}])([A-Z][A-Z0-9_]{{1,23}})(?![{_B}])")


def _token(value: str) -> str:
    """把一個值包成「整詞」樣式。"""
    return f"(?<![{_B}]){re.escape(value)}(?![{_B}])"

_COLUMNS_SQL = """
SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE, COLUMN_COMMENT
FROM INFORMATION_SCHEMA.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND COLUMN_COMMENT <> ''
ORDER BY TABLE_NAME, ORDINAL_POSITION
"""


_GT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "eval_ground_truth.yaml",
)


def _load_gt() -> list[tuple]:
    """(題號, expect, 這題所有 gold SQL 串在一起)。載不到就回空 —— 落差照報。"""
    try:
        data = yaml.safe_load(io.open(_GT_PATH, encoding="utf-8").read())
    except Exception as e:
        print(f"!! 讀不到 {_GT_PATH}（{type(e).__name__}: {e}），死代碼只能報成未分類")
        return []
    items = data if isinstance(data, list) else list(data.values())[0]
    out = []
    for q in items:
        sqls = " ".join([q.get("sql") or ""] + list(q.get("alt_sql") or []))
        out.append((q.get("id"), q.get("expect"), sqls))
    return out


def gt_verdict(gt: list[tuple], col: str, value: str) -> tuple[str, list]:
    """
    「註解宣告了、資料 0 筆」到底是不是缺陷 —— **問題庫，不要自己猜**。

    這一層是 [[fix-the-detector-not-the-instance]]：上一版把 9 個死代碼
    全報成待查缺陷，但其中 customer_profiles.risk_flag 的 HIGH 是 `#159`
    （`expect: empty`，capability 寫著「誠實回報空集合」）**賴以存在的東西**，
    payments.status 的 FAILED 同樣是 `#104`。把資料補進去等於刪掉那兩題。

    所以判定要看引用它的題目期望什麼：
      撐題    有 expect: empty 的題目打在這個值上 → 題庫的設計，不准動資料
      缺陷    有 expect: rows  的題目打在這個值上 → 真的壞了，要修
      無人引用 沒有題目提到    → 無害，一行帶過
    """
    hits = [(qid, expect) for qid, expect, sqls in gt
            if re.search(_token(col), sqls) and f"'{value}'" in sqls]
    if not hits:
        return "無人引用", []
    if any(e == "empty" for _, e in hits):
        return "撐題", hits
    return "缺陷", hits


def describe(comment: str) -> str:
    """取註解的前導描述 —— 冒號前，或括號前。代碼清單本身不重複進來。"""
    for sep in ("：", ":"):
        if sep in comment:
            return comment.partition(sep)[0].strip()
    return re.split(r"[（(]", comment)[0].strip() or comment.strip()


def gloss_for(comment: str, value: str) -> str:
    """
    在註解裡找 `value`，取它後面到下一個分隔符為止的中文當注解。

    **錨點是資料裡的真值**，不是註解裡長得像代碼的 token。
    這樣兩種註解風格都吃得下，而且不會把「A/B 測試」的 A 和 B 當成代碼。
    """
    m = re.search(_token(value), comment)
    if not m:
        return ""
    tail = comment[m.end():]
    # 中文注解一定隔一個空白：「DECLINED 銀行拒絕」。
    # 不是空白就代表後面那串不屬於這個值 —— 「1~5級」的 `~5級` 不是 1 的注解。
    if not tail[:1].isspace():
        return ""
    tail = _SEP_RE.split(tail)[0].strip()
    return tail.split()[0] if tail.split() else ""


def is_closed_set(vals: list, n_rows: int) -> bool:
    """
    是不是一個封閉的小集合 —— **看資料，不看註解**。

    兩個條件都要：
      值夠少          超過 MAX_VALUES 就不是可窮舉的集合，是自由文字
      重複得夠明顯    列數至少是相異值的 3 倍。否則「10 列剛好 8 個相異值」
                      這種接近唯一的欄位（識別碼、人名）會被誤收
    """
    if not (2 <= len(vals) <= MAX_VALUES):
        return False
    if any(v is None or len(str(v)) > MAX_LEN for v in vals):
        return False
    return n_rows >= 3 * len(vals)


def build() -> tuple[dict, list[tuple[str, str]], list[str], dict[str, str]]:
    db = get_db_manager(MYSQL_URI)
    existing_data = (yaml.safe_load(io.open(YAML_PATH, encoding="utf-8").read())
                     .get("enum_fields") or {})
    existing = set(existing_data)

    gt = _load_gt()

    out: dict[str, dict] = {}
    notes: list[tuple[str, str]] = []   # (判定, 說明) —— 判定見 gt_verdict
    candidates: list[str] = []  # 是封閉集合、但註解沒宣告 —— 射程外，只報告
    kinds: dict[str, str] = {}  # key -> R1 黏連 / R2 未列 / R3 乾淨

    with db.engine.connect() as conn:
        rows = conn.execute(text(_COLUMNS_SQL)).fetchall()
        n_rows_cache: dict[str, int] = {}

        for table, col, col_type, comment in rows:
            key = f"{table}.{col}"
            t = col_type.lower()
            if not t.startswith("enum") and not (
                t.startswith(("varchar", "char")) and _width(t) <= MAX_LEN
            ):
                continue

            if table not in n_rows_cache:
                n_rows_cache[table] = conn.execute(
                    text(f"SELECT COUNT(*) FROM `{table}`")).scalar() or 0
            n_rows = n_rows_cache[table]

            vals = [r[0] for r in conn.execute(text(
                f"SELECT DISTINCT `{col}` FROM `{table}` "
                f"WHERE `{col}` IS NOT NULL ORDER BY 1 LIMIT {MAX_VALUES + 1}"
            )).fetchall()]

            # 現有的 6 個是手寫的，帶著跨表歧義警告（四個同名 status），
            # 不能被生成版蓋掉。但仍然拿真實值對帳一次 —— 免費的檢查。
            #
            # ⚠️ 這段要在 is_closed_set 之前。`payments.status` 資料裡只剩
            # 一個相異值（164 筆全是 SUCCESS），`2 <= len(vals)` 那條會先把它
            # 濾掉 —— 於是**最該被報出來的那個形狀，恰好是閘門看不到的那個**。
            if key in existing:
                declared_old = set(
                    (existing_data[key].get("values") or {}).keys())
                for d in sorted(declared_old - set(map(str, vals))):
                    verdict, qs = gt_verdict(gt, col, d)
                    notes.append((verdict, f"[手寫] {key} 列了 '{d}'、資料 0 筆"
                                  + (f" ← {[q for q, _ in qs]}" if qs else "")))
                for u in sorted(set(map(str, vals)) - declared_old):
                    notes.append(("註解沒提", f"[手寫] {key} 資料有 '{u}'、沒列"))
                continue

            if not is_closed_set(vals, n_rows):
                continue

            cm = comment or ""
            sval = [str(v) for v in vals]
            # **納不納入由註解決定，值由資料決定。**
            #
            # §9.12 的病灶很窄：註解把「代碼 中文」黏成一串，模型讀成一個值
            # （`channel = 'APP 行動應用'`）。所以要修的是「作者已經寫成代碼清單」
            # 的欄位。純看資料會失控 —— 種資料時人名只抽了 4 個、銀行名只抽了 12 個，
            # 在這份資料裡它們也是封閉集合，但那是**種子池的產物，不是欄位的語意**，
            # 宣告成 enum 等於告訴模型一件不真的事。
            declared = set(_CODEISH.findall(cm))
            # 純數字的值不算「代碼」。energy_label 的註解是「等級 1~5級」，
            # 1 和 5 會在範圍式裡被命中，於是整欄被誤判成有宣告的代碼清單。
            hit = [v for v in sval
                   if not _NUMERIC.match(v) and re.search(_token(v), cm)]
            if len(hit) < 2:
                # 是封閉集合、但註解沒宣告 —— 只報告，讓人決定要不要擴大射程。
                candidates.append(f"{key}：{sval}  註解「{describe(cm) or cm}」沒宣告值")
                continue

            dead = sorted(declared - set(sval))
            unlisted = [v for v in sval if v not in hit]
            for d in dead:
                verdict, qs = gt_verdict(gt, col, d)
                notes.append((verdict, f"{key} 註解宣告 '{d}'、資料 0 筆"
                              + (f" ← {[q for q, _ in qs]}" if qs else "")))
            if unlisted:
                notes.append(("註解沒提",
                              f"{key} 資料有 {unlisted}、註解沒提 —— 模型無從得知"))

            # 值域 = 資料 ∪ 註解宣告。**兩邊都要，理由不同**：
            #   資料   決定**字面形式** —— §9.12 的病灶是 'APP 行動應用'，
            #          不是 'APP'。這一半非有不可，註解自己說不清楚。
            #   註解   決定**值域**。REFUNDED 現在 0 筆不代表它不合法；
            #          砍掉它模型會退而求其次挑一個錯的值。手寫的
            #          payments.status（列了 3 個、資料只有 1 個）就是這樣寫的。
            # 順序：資料先（照 ORDER BY），註解獨有的接在後面。
            values = {v: gloss_for(cm, v) for v in sval}
            for d in dead:
                values[d] = gloss_for(cm, d)

            out[key] = {"description": describe(cm), "values": values}

            # 分級 —— **決定這一欄值不值得進 A/B 的 B 臂**。
            #   R1 黏連  註解把「代碼 中文」黏成一串（`WEB 官網／APP 行動應用`）。
            #            §9.12 的病灶就是這個形狀：模型寫出 'APP 行動應用'。
            #   R2 未列  資料有、註解完全沒提 —— 模型無從得知。
            #   R3 乾淨  `工單狀態 (OPEN/PENDING/CLOSED)`，代碼之間是分隔符，
            #            沒有可黏的中文。enum 只是把 DDL 已經有的東西再抄一遍。
            # 判準直接沿用 gloss_for：它取得到中文，就代表中文黏在值後面。
            kinds[key] = ("R2" if unlisted else
                          "R1" if any(values[v] for v in sval) else "R3")

    return out, notes, candidates, kinds


def _width(col_type: str) -> int:
    m = re.search(r"\((\d+)\)", col_type)
    return int(m.group(1)) if m else 9999


def render(out: dict) -> str:
    """排版對齊現有手寫區塊的樣式（兩空格縮排、值再縮兩層）。"""
    lines = [BEGIN]
    for key, info in out.items():
        lines.append(f"  {key}:")
        desc = str(info["description"]).replace('"', "'")
        lines.append(f'    description: "{desc}"')
        lines.append("    values:")
        w = max((len(v) for v in info["values"]), default=0) + 1
        for val, gloss in info["values"].items():
            g = str(gloss).replace('"', "'")
            lines.append(f'      {val + ":":<{w + 1}} "{g}"')
    lines.append(END)
    return "\n".join(lines) + "\n"


def main() -> int:
    out, notes, candidates, kinds = build()

    tally = {k: [f for f, v in kinds.items() if v == k] for k in ("R1", "R2", "R3")}
    print(f"分級：R1 黏連 {len(tally['R1'])}｜R2 未列 {len(tally['R2'])}｜"
          f"R3 乾淨 {len(tally['R3'])}　→ 最小集 {len(tally['R1']) + len(tally['R2'])} 欄")

    if "--minset" in sys.argv:
        path = sys.argv[sys.argv.index("--minset") + 1]
        keys = tally["R1"] + tally["R2"]
        io.open(path, "w", encoding="utf-8", newline="\n").write(
            "# gen_enum_fields.py --minset 產生，勿手動編輯\n"
            "# R1 黏連（註解把「代碼 中文」黏成一串）＋ R2 未列（資料有、註解沒提）。\n"
            "# R3「乾淨」的欄位刻意不收 —— 見 --only 的說明。\n"
            + "\n".join(keys) + "\n")
        print(f"已寫入 {path}：{len(keys)} 個欄位")
        return 0

    # `--only`：只留白名單裡的欄位。**這不是省字元，是為了留下對照組。**
    # 全量 74 欄會動到 150/309 題的 Prompt；砍掉 28 個「乾淨」欄位之後
    # 只動 86 題，其餘 223 題的 Prompt **位元不變** —— 那 223 題就是免費的
    # 雜訊地板（[[single-layer-interventions-make-their-own-control]]）。
    # 沒有它，「不會變差」這個宣稱在現有配額下量不出來。
    if "--only" in sys.argv:
        path = sys.argv[sys.argv.index("--only") + 1]
        body = "\n".join(ln for ln in io.open(path, encoding="utf-8")
                         if not ln.lstrip().startswith("#"))
        keep = {k.strip() for k in re.split(r"[,\s]+", body) if k.strip()}
        missing = keep - set(out)
        assert not missing, f"--only 名單裡有 {len(missing)} 個不在產出中: {sorted(missing)}"
        dropped = len(out) - len(keep)
        out = {k: v for k, v in out.items() if k in keep}
        print(f"--only {path}：留 {len(out)} 欄，濾掉 {dropped} 欄\n")

    block = render(out)

    print(f"產生 {len(out)} 個 enum 欄位，共 {len(block):,} 字元\n")

    # 第一行就報缺陷數。清單長不長不重要，**有沒有真的壞掉**才重要
    # （[[gates-measure-exposure-not-defects]]、[[silent-pass-is-not-a-pass]]）。
    bad = [t for v, t in notes if v == "缺陷"]
    print(f"落差 {len(notes)} 筆，其中**真缺陷 {len(bad)} 筆**"
          f"（有 expect: rows 的題目打在一個 0 筆的值上）")
    for verdict in ("缺陷", "撐題", "註解沒提", "無人引用"):
        group = [t for v, t in notes if v == verdict]
        if not group:
            continue
        tail = {
            "缺陷": "← 要修",
            "撐題": "← **題庫的設計，不准動資料**",
            "註解沒提": "← 模型無從得知",
            "無人引用": "← 無害，沒有題目碰它",
        }[verdict]
        print(f"\n  【{verdict}】{len(group)} 筆 {tail}")
        for t in group:
            print(f"    {t}")
    print()
    if candidates:
        print(f"—— 射程外：是封閉集合但註解沒宣告代碼（{len(candidates)} 筆，未納入）——")
        for c in candidates:
            print(f"  {c}")
        print()

    dest = YAML_PATH
    if "--out" in sys.argv:
        dest = sys.argv[sys.argv.index("--out") + 1]
    elif "--write" not in sys.argv:
        print(block[:2000])
        print(f"\n...（預覽前 2000 字元）\n"
              f"加 --write 寫回 {YAML_PATH}，或 --out PATH 產 A/B 的 B 臂")
        return 0

    raw = io.open(YAML_PATH, encoding="utf-8").read()

    if BEGIN in raw:                       # 重跑：換掉上次產生的區段
        start = raw.index(BEGIN)
        end = raw.index(END) + len(END) + 1
        new = raw[:start] + block + raw[end:]
    else:                                  # 首次：插在 enum_fields 區塊的結尾
        marker = "\nenum_fields:\n"
        if marker not in raw:
            print("找不到 enum_fields: 區塊，請手動處理")
            return 1
        # 手寫項一路到下一個頂層區塊（以 `# ===` 開頭的區隔線）為止
        after = raw.index(marker) + len(marker)
        nxt = raw.index("\n# ====", after)
        new = raw[:nxt] + "\n" + block + raw[nxt:]

    # 寫回去要能被 yaml 解析，而且手寫的 6 個必須還在 —— 靜默寫壞
    # 比不寫更糟（[[silent-pass-is-not-a-pass]]）。
    parsed = yaml.safe_load(new)
    got = parsed.get("enum_fields") or {}
    assert len(got) >= len(out) + 6, f"寫回後 enum_fields 只剩 {len(got)} 個，中止"
    for k in ("orders.status", "payments.status", "shipments.status"):
        assert k in got, f"手寫項 {k} 不見了，中止"

    io.open(dest, "w", encoding="utf-8", newline="\n").write(new)
    print(f"已寫入 {dest}：enum_fields {len(got) - len(out)} → {len(got)} 個")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
