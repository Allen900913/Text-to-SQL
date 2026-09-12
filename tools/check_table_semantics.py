# -*- coding: utf-8 -*-
"""閘門：表註解的分句型宣告有沒有跟現實對上（零 LLM）

為什麼需要這一支
====================================================================
`utils/table_semantics.yaml` 現在是表註解的唯一來源，三個消費端各拿各的投影。
這帶來兩個**新的靜默失敗**，各要一項檢查：

  ① YAML 與資料庫脫節。改了 YAML 沒同步，或建了新表沒進 YAML ——
     檢索與目錄讀 YAML、DDL 讀資料庫，兩邊會安靜地講不同的話。

  ② **建表腳本裡的註解變成死文字。** 遷移之前，改 init_db.py 的 COMMENT
     再跑 sync 就會生效；遷移之後 YAML 才算數，改建表腳本**什麼都不會發生**。
     這是「新增了一個來源卻沒接上產生鏈」的鏡像 —— 舊來源還在，但已經斷了。

②那一項刻意**用枚舉不用登記表**。原本的 `SOURCES / TUPLE_SRCS / YAML_SRCS`
是手寫清單，而它已經漏過一次：`tools/wide_table_plan_2.yaml` 的 7 張表
（shipment/review/payment/support_ticket/subscription/promotion/return_profiles）
從來沒被登記，症狀是「改了註解不生效」，而閘門 [3] 照樣報 OK ——
**沒登記的來源不可能不一致，因為沒有人看它。** 手寫清單抓不到自己漏了什麼，
所以這裡改成掃磁碟：db/*.py、tools/add_*.py、tools/*plan*.yaml 全掃。

用法：
    python tools/check_table_semantics.py
    python tools/check_table_semantics.py --all    # 連對上的也列出來
"""
import glob
import io
import os
import re
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.config import MYSQL_URI  # noqa: E402
from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.table_semantics import (  # noqa: E402
    KINDS, PROJECTIONS, briefs_for, load,
)
from langgraph_sql.utils.table_semantics import columns as ts_columns  # noqa: E402
from langgraph_sql.utils.table_semantics import enums as ts_enums  # noqa: E402

_SQL = re.compile(
    r"CREATE TABLE(?:\s+IF NOT EXISTS)?\s+(\w+)\s*\([^;]*?\)\s*COMMENT\s*'([^']*)'\s*;", re.S)
# Python tuple 宣告：("表名", "表註解", """欄位…""")
_TUP = re.compile(r'\(\s*"(\w+)"\s*,\s*"([^"]*)"\s*,\s*"""', re.S)


def _norm(s: str) -> str:
    return " ".join((s or "").split())


def scan_sources() -> dict[str, list[tuple[str, str]]]:
    """掃磁碟找所有宣告過表註解的地方。**不用手寫登記表** —— 手寫清單
    抓不到自己漏了什麼，而漏掉的症狀是「改了不生效」，完全靜默。"""
    found: dict[str, list[tuple[str, str]]] = {}
    pats = [os.path.join(_ROOT, "db", "*.py"),
            os.path.join(_ROOT, "tools", "add_*.py"),
            os.path.join(_ROOT, "tools", "*plan*.yaml")]
    for pat in pats:
        for path in sorted(glob.glob(pat)):
            rel = os.path.relpath(path, _ROOT)
            src = io.open(path, encoding="utf-8").read()
            hits: list[tuple[str, str]] = []
            if path.endswith(".yaml"):
                doc = yaml.safe_load(src) or {}
                tabs = doc.get("tables") or {}
                if isinstance(tabs, dict):
                    hits = [(t, (spec or {}).get("table_comment", ""))
                            for t, spec in tabs.items()
                            if isinstance(spec, dict) and spec.get("table_comment")]
            else:
                hits = _SQL.findall(src) + _TUP.findall(src)
            if hits:
                found[rel] = [(t.lower(), _norm(c)) for t, c in hits]
    return found


def main() -> int:
    show_all = "--all" in sys.argv
    db = get_db_manager(MYSQL_URI)
    with db.engine.connect() as conn:
        live = {t.lower(): (c or "").strip() for t, c in conn.execute(text(
            "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_TYPE = 'BASE TABLE'")).fetchall()}
    data = load()
    fails = 0

    # ── [1] 覆蓋 ────────────────────────────────────────────────────
    missing, extra = sorted(set(live) - set(data)), sorted(set(data) - set(live))
    ok = not missing and not extra
    print(f"[1] 覆蓋       {'OK' if ok else 'FAIL'}（YAML {len(data)} / 資料庫 {len(live)}）")
    if missing:
        print(f"    ✗ 資料庫有、YAML 沒有：{missing}")
    if extra:
        print(f"    ✗ YAML 有、資料庫沒有：{extra}")
    fails += 0 if ok else 1

    # ── [2] 句型合法 ────────────────────────────────────────────────
    badkind = [(t, c["kind"]) for t, cl in data.items() for c in cl
               if c["kind"] not in KINDS]
    print(f"[2] 句型合法   {'OK' if not badkind else 'FAIL'}"
          f"（{sum(len(c) for c in data.values())} 句，{len(KINDS)} 種句型）")
    for t, k in badkind[:5]:
        print(f"    ✗ {t}: {k}")
    fails += 0 if not badkind else 1

    # ── [3] ddl 投影 == 資料庫（表 ＋ 欄位）─────────────────────────
    ddl = briefs_for("ddl")
    drift = [t for t in live if t in ddl and ddl[t] != live[t]]
    print(f"[3] ddl 投影   {'OK' if not drift else 'FAIL'}"
          f"（{len(live) - len(drift)}/{len(live)} 與 TABLE_COMMENT 逐位元相同）")
    for t in drift[:5]:
        print(f"    ✗ {t}\n      YAML {ddl[t]}\n      資料庫 {live[t]}")
    fails += 0 if not drift else 1

    with db.engine.connect() as conn:
        livecols = {(t.lower(), n.lower()): (c or "").strip() for t, n, c in conn.execute(
            text("SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT FROM "
                 "INFORMATION_SCHEMA.COLUMNS WHERE TABLE_SCHEMA = DATABASE()"))}
    ycols = ts_columns()
    cmiss = sorted(set(livecols) - set(ycols))
    cdrift = [k for k in set(livecols) & set(ycols) if livecols[k] != ycols[k]]
    cok = not cmiss and not cdrift
    print(f"[3b] 欄位註解  {'OK' if cok else 'FAIL'}"
          f"（YAML {len(ycols)} / 資料庫 {len(livecols)}，"
          f"{len(livecols) - len(cmiss) - len(cdrift)}/{len(livecols)} 逐位元相同）")
    for k in (cmiss + cdrift)[:5]:
        print(f"    ✗ {k[0]}.{k[1]}")
    fails += 0 if cok else 1

    # ── [4] 建表腳本已成死文字，有沒有人還在改它 ────────────────────
    srcs = scan_sources()
    stale, seen = [], set()
    for rel, hits in srcs.items():
        for t, c in hits:
            seen.add(t)
            if t in ddl and _norm(ddl[t]) != c:
                stale.append((rel, t))
    print(f"[4] 舊來源     {'OK' if not stale else 'WARN'}"
          f"（掃到 {len(srcs)} 支來源、{len(seen)} 張表；YAML 才算數）")
    for rel, t in stale[:10]:
        print(f"    ⚠ {rel} 的 {t} 與 YAML 不一致 —— 改那裡不會生效，請改 YAML")
    if show_all:
        for rel, hits in srcs.items():
            print(f"    · {rel:<42} {len(hits)} 張表")
    nosrc = sorted(set(ddl) - seen)
    if nosrc:
        print(f"    · 沒有任何建表腳本宣告的表 {len(nosrc)} 張（正常：YAML 是來源）")

    # ── [4b] 值域 vs 真實資料 ───────────────────────────────────────
    #
    # 這一項就是 enum 從「產物」變成「宣告」之後該有的新鮮度閘門。
    # 舊做法沒有：[2] 只重跑 gen_ddl.py 比對 DDL，enum_fields 沒有對應項，
    # 於是線上 52 欄與產生器今天重跑得到的 0 欄可以無聲地並存好幾個月。
    # 現在值域是宣告的，新鮮度就退化成一個直接的問題：**宣告的值，資料裡有嗎。**
    # 這一整項的前提：**資料庫裡沒有任何 schema 層的值域宣告。**
    #
    # 為什麼前提重要（2026-09-12 查證）
    #   · 欄位是 ENUM 型別  → 值域寫在 column specification 裡，宣告時決定，
    #     與資料無關。MySQL 手冊要人用 `SHOW COLUMNS` 解析 Type 欄來取得
    #     「所有可能值」，而不是 SELECT DISTINCT。這種情況下「宣告了、
    #     資料 0 筆」是**完全正常的**，那個值是合法的，告訴模型它存在是真話。
    #   · 欄位是 VARCHAR    → schema 沒有宣告任何值域（只有長度／字元集／
    #     collation）。那 YAML 的 enums 就不是契約的副本，是**我們對資料的
    #     描述** —— 描述裡寫一個資料 0 筆的值，就是一句假話。
    #   · CHECK constraint（8.0.16+）是第三種 schema 層值域，獨立於型別。
    #
    # 今天全庫 0 個 ENUM／0 個 SET／0 個 CHECK，所以判準是「資料裡有嗎」。
    # 只要有一個出現，這個判準就得反過來（那時該驗的是「宣告涵不涵蓋 DDL
    # 的值域」）。所以前提寫成會爆的檢查，不寫成註解 —— 沒觸發不等於沒有。
    with db.engine.connect() as conn:
        declared = conn.execute(text(
            "SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE IN ('enum','set')")).scalar()
        try:
            checks = conn.execute(text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.CHECK_CONSTRAINTS "
                "WHERE CONSTRAINT_SCHEMA = DATABASE()")).scalar()
        except Exception:
            checks = 0
    # 2026-09-12 前提真的翻了：98 欄型別化成 ENUM。判準跟著反過來 ——
    #
    #   舊：宣告的值資料裡有嗎（VARCHAR 時代，YAML 是「我們對資料的描述」，
    #       描述裡寫一個資料 0 筆的值就是假話）
    #   新：**值域的權威是 COLUMN_TYPE**，宣告了而資料 0 筆是完全正常的
    #       （enum-declares-domain-not-snapshot）。要驗的變成兩件事：
    #         ① 同一欄不准在兩個地方各宣告一次 —— 會漂移，而且值會送兩遍
    #         ② 型別上的值域要真的到得了模型 —— DDL 是生成的，
    #            改了型別沒重跑 gen_ddl 就會靜默失血
    #            （memory: schema-source-must-be-wired）
    ec = ts_enums()
    typed = {}
    with db.engine.connect() as conn:
        for tb, co, ct in conn.execute(text(
                "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND DATA_TYPE IN ('enum','set')")):
            typed["%s.%s" % (tb, co)] = ct
    both = sorted(set(typed) & set(ec))
    try:
        from langgraph_sql.utils.schema_parser import get_schema_parser
        ddl_txt = get_schema_parser().get_ddl()
    except Exception:
        ddl_txt = ""
    unwired = sorted(k for k, ct in typed.items()
                     if ct.split("(")[0].upper() + "(" not in ddl_txt.upper()
                     or k.split(".")[1] + " " not in ddl_txt.replace("\t", " "))
    n_enum_ddl = ddl_txt.upper().count("ENUM(")
    verdict4b = "FAIL" if (both or n_enum_ddl != len(typed)) else "OK"
    print(f"[4b] 值域的家 {verdict4b}（型別上 {len(typed)} 欄、CHECK {checks} 條；"
          f"DDL 裡 {n_enum_ddl} 行 ENUM(；YAML 還留 {len(ec)} 欄）")
    if both:
        fails += 1
        print(f"    ✗ {len(both)} 欄同時宣告在型別與 YAML —— 值會送兩遍，而且兩份會漂移：")
        for k in both[:8]:
            print(f"       {k}")
        print("      改法：型別是權威，把 YAML 的 enums 宣告刪掉。")
    if n_enum_ddl != len(typed):
        fails += 1
        print(f"    ✗ 資料庫有 {len(typed)} 欄 ENUM，DDL 只有 {n_enum_ddl} 行 —— "
              "改了型別沒重跑 `python tools/gen_ddl.py --write`，模型看到的還是舊的。")
    if ec:
        print(f"    · YAML 還留著 {len(ec)} 欄：那些是刻意不當值域的"
              "（外部標準碼、會長大的登記簿），值走值索引不走型別。")
    ec = {}            # 下面那段死值檢查整個退役 —— 判準已經翻掉了
    dead, unlisted, sentinel, registered = [], [], [], []
    with db.engine.connect() as conn:
        for key, info in (ec or {}).items():
            t, _, col = key.partition(".")
            try:
                vals = {str(r[0]) for r in conn.execute(text(
                    f"SELECT DISTINCT `{col}` FROM `{t}` WHERE `{col}` IS NOT NULL"))}
            except Exception:
                continue
            decl = set(map(str, (info.get("values") or {})))
            # 死值要**登記**才放行。登記寫在宣告裡（`dead_ok: {值: 理由}`），
            # 不寫成閘門內部的欄位清單 —— 同哨兵那條的理由。
            #
            # ⚠️ 2026-09-12：這一段以前只把 dead 算出來、印在摘要行裡，
            # 紅綠燈只看 unlisted。於是 14 個死值以「一個沒有人會去讀的數字」
            # 的形式公開存在了很久，其中 `payments.status='REFUNDED'` 讓
            # 風格驗證集 #3046 拿到一個看起來完全合法的 0 列。
            # **死值的代價不是多送幾個字，是把失敗變靜默。**
            okv = set(map(str, (info.get("dead_ok") or {})))
            for v in sorted(decl - vals):
                if v in okv:
                    registered.append(f"{key} '{v}' —— {info['dead_ok'][v]}")
                else:
                    dead.append(f"{key} 宣告 '{v}'、資料 0 筆")
            # 哨兵欄位：值域**本來就不封閉**（`defect_code` 的 DF-NN、
            # `mon_open` 的時段字串、`member_tier_required` 的等級名稱），
            # 宣告只列哨兵是對的，不是漏列。認的是 description 裡的「只列哨兵」
            # —— 用宣告自己說出來的意圖當豁免條件，不在閘門裡寫一份表名清單，
            # 那種清單會跟資料一起過期而且不會有人發現。
            #
            # 為什麼要有這個豁免：常態亮著的 WARN 會被當背景雜訊，
            # 兩個月後真的漏列一個活值時沒有人會注意到
            # （memory: `silent-pass-is-not-a-pass` 的反面 —— 假的警告
            # 跟吞掉的例外一樣會讓閘門失去意義）。
            if "只列哨兵" in (info.get("description") or ""):
                sentinel.append(f"{key}（哨兵 {sorted(decl)}、開放集合 {len(vals - decl)} 種）")
                continue
            for v in sorted(vals - decl):
                unlisted.append(f"{key} 資料有 '{v}'、宣告沒列")
    # 未登記的死值是 FAIL，不是 WARN。漏列仍是 WARN —— 那是「模型寫不出
    # 這個條件」（少給），死值是「模型寫得出一個不存在的條件」（給錯）。
    # 給錯比少給嚴重：少給會拿到空手，給錯會拿到一個看起來合法的答案。
    if dead:
        fails += 1
    # 前提破了（ec is None）就不要再印第二行摘要 —— 那行會寫
    # 「OK（0 個欄位）」，把上面的 FAIL 蓋掉。**0 個欄位是「沒掃」，
    # 不是「掃過沒事」**，而一個報假乾淨的閘門比沒有閘門更糟。
    # ec 被上面清成 {} —— 死值檢查退役之後這行摘要不該再印，
    # 否則會在 [4b] 的判決底下再蓋一行「OK（0 個欄位）」。
    if ec:
        verdict = "FAIL" if dead else ("WARN" if unlisted else "OK")
        print(f"[4b] 值域新鮮度 {verdict}"
              f"（{len(ec)} 個欄位；未登記死代碼 {len(dead)}、漏列 {len(unlisted)}"
              + (f"、已登記 {len(registered)}" if registered else "")
              + (f"、哨兵 {len(sentinel)}" if sentinel else "") + "）")
    for m in dead:
        print(f"    ✗ {m} —— 系統會告訴模型這個值存在，它不存在")
    if dead:
        print("      改法：清掉它；若是題庫刻意撐 expect: empty 的題，"
              "在宣告裡加 `dead_ok: {值: 理由}` 登記（查 GT 的 expect 再動）。")
    for m in unlisted[:8]:
        print(f"    ⚠ {m} —— 模型寫不出這個條件")
    for m in registered:
        print(f"    · {m}")
    for m in sentinel:
        print(f"    · {m} —— 值域不封閉，只驗哨兵值還在不在")

    # ── [4c] 散文裡的代碼 vs 真實資料 ───────────────────────────────
    #
    # [4b] 只驗 `enums:` 區塊。但代碼還住在**另一個地方**：表註解的
    # `kind: enum` 子句（「付款狀態 SUCCESS/FAILED/REFUNDED」）。那一句
    # 不進 ddl 投影（代碼離開資料庫註解是為了解鎖值索引），但**catalog 與
    # retrieval 都收**，所以它會到達選表 LLM 與餘弦。
    #
    # ⚠️ 2026-09-12：清掉 `enums:` 區塊的 REFUNDED 之後，payments 的
    # enum 子句還寫著 SUCCESS/FAILED/REFUNDED —— **同一個檔案裡兩個地方
    # 互相矛盾**，而且沒有任何東西在看它（redundancy-ok-contradiction-not）。
    # 冗餘可以，矛盾不行。
    # 先把 [4b] 的登記按表分組，[4c] 要吃同一份豁免。
    ts_by_table = {}
    for _k, _v in ts_enums().items():
        _t, _, _c = _k.partition(".")
        ts_by_table.setdefault(_t, {})[_c] = _v
    codes, stale = re.compile(r"\b[A-Z][A-Z0-9_]{2,}\b"), []
    with db.engine.connect() as conn:
        for t, clauses in data.items():
            said = {c for cl in clauses if cl["kind"] == "enum"
                    for c in codes.findall(cl["text"])}
            if not said:
                continue
            # 「活」的定義：**型別上宣告過的值，或資料裡真的有的值**。
            # 2026-09-12 型別化之後前者才是主要來源 —— 只看 varchar 的資料，
            # `orders.status` 這種已經是 ENUM 的欄位會整串代碼被誤判成死的。
            live = set()
            for col, dt, ct in conn.execute(text(
                    "SELECT COLUMN_NAME, DATA_TYPE, COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t "
                    "AND DATA_TYPE IN ('varchar','char','enum','set')"), {"t": t}):
                if dt in ("enum", "set"):
                    live |= set(re.findall(r"'((?:[^']|'')*)'", ct))
                    continue
                live |= {str(r[0]) for r in conn.execute(text(
                    "SELECT DISTINCT `%s` FROM `%s` WHERE `%s` IS NOT NULL"
                    % (col, t, col)))}
            # 吃 [4b] 的同一份登記。承重的死值（撐 expect: empty 的題）
            # 在散文裡也該留著 —— 生成器靠 enums 寫得出那個 WHERE，
            # 選表 LLM 靠這句話知道該挑這張表。同一個理由，同一份豁免。
            live |= {v for info in (ts_by_table.get(t) or {}).values()
                     for v in (info.get("dead_ok") or {})}
            stale += [(t, c) for c in sorted(said - live)]
    print(f"[4c] 散文裡的代碼 {'OK' if not stale else 'FAIL'}"
          f"（{sum(1 for cl in data.values() for c in cl if c['kind'] == 'enum')} 句 enum 子句）")
    for t, c in stale:
        print(f"    ✗ {t} 的註解寫了 '{c}'、資料 0 筆 —— 這句話會進 catalog 與 retrieval")
    fails += 1 if stale else 0

    # ── [5] 生效投影 ────────────────────────────────────────────────
    # 「預設」是**原始碼裡登記的那組**，不是「三個角色全收」。
    # 2026-09-10 起 ddl 砍掉 enum 就是預設（代碼的家在 enum_fields，
    # 留在 TABLE_COMMENT 會把自己的值從值索引踢掉）。這裡要分辨的是
    # 「環境變數覆寫了嗎」—— 那才是 A/B 臂（memory: ab-arms-pin-to-a-version）。
    env = {r: os.environ.get("TS_" + r.upper()) for r in PROJECTIONS}
    armed = {r: v for r, v in env.items() if v}
    print(f"[5] 生效投影   " + ("預設（原始碼登記的那組）" if not armed else
                             f"⚠ 環境變數覆寫 {sorted(armed)} —— 這是 A/B 臂"))
    for r, ks in PROJECTIONS.items():
        drop = [k for k in KINDS if k not in ks]
        print(f"    {r:<10} {'、'.join(ks)}" + (f"   ✂ 砍掉 {drop}" if drop else ""))
    if armed:
        for r, ks in PROJECTIONS.items():
            if "ptr" not in ks:
                n = sum(1 for cl in data.values() for c in cl if c["kind"] == "ptr")
                print(f"    ⚠ {r} 少了 {n} 句指路標 —— 檢索該砍（餘弦不懂否定），"
                      f"目錄不該砍（#286/#287 靠它知道還需要母表）")

    print("\n" + "=" * 66)
    print("全部通過。" if not fails else f"{fails} 項失敗。")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
