# -*- coding: utf-8 -*-
"""保留驗收集的守門員 —— 封存**之前**必須全綠。

這份題庫的用途只有一個：量「這個架構在沒參與過任何決策的題上有多準」。
所以它跟 `eval_ground_truth.yaml`（開發集）有一條硬界線：

    開發集   跑完可以看錯題、可以據此改架構 —— 它就是拿來調的
    驗收集   跑完**只准看總分與事前登記的切面**。錯題清單不准拿來改任何
             東西（註解、參數、few-shot、檢索都不行）。看了就污染了。

因此所有品質問題都要在**封存之前**解決 —— 封存之後才發現某題 GT 錯了，
那題**作廢**（記進 void 清單、從分母拿掉），不是修好再跑一次。
修完再跑等於用結果挑題目。

六項檢查
================================================================
[1] GT 可執行        每題的 SQL 真的跑得動
[2] expect 相符      rows 要有列、empty 要 0 列
[3] 不與開發集重複    跟 309 題的字元三元組相似度上限
[4] **註解洩漏**     問句與目標表註解的最長共同子字串 —— 見下
[5] 分布相符         表數／形狀／寬窄要打中事前登記的配額
[6] 封存             內容 hash，之後每跑一次記一筆

[4] 為什麼是重點
----------------------------------------------------------------
出題的人如果看著 schema 註解寫問句，問句用詞就會跟註解逐字對應，量到的
就變成「註解抄得像不像」而不是「架構好不好」。開發集的 `#282`
（問句「中途改過地址」／註解「是否中途改過送件地址」）正是這個形狀 ——
它讓欄位提示那一層看起來比實際強。

新題庫用 `tools/make_data_cards.py` 的資料卡出題（卡片不含任何註解），
但寫題的人如果讀過註解就不可能真的隔離，所以這裡用機械檢查兜底：
問句與 GT 表的表註解／欄位註解取最長共同**連續**子字串（連續而非 LCS ——
抄註解會整段一樣），超過門檻就退回重寫。
"""
import hashlib
import io
import os
import re
import sys
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import yaml  # noqa: E402
from loguru import logger as log  # noqa: E402
from sqlalchemy import text  # noqa: E402

from langgraph_sql.utils.db_manager import get_db_manager  # noqa: E402
from langgraph_sql.utils.value_index import MYSQL_URI  # noqa: E402

TESTSET = os.path.join(_ROOT, "eval", "testset_holdout.yaml")
DEVSET = os.path.join(_ROOT, "eval_ground_truth.yaml")
RUNLOG = os.path.join(_ROOT, "eval", "testset_runlog.json")

LEAK_MAX = 6        # 問句與註解的最長共同子字串上限（字數）
DUP_MAX = 0.62      # 與開發集任一題的字元三元組 Jaccard 上限

# 事前登記的配額（照開發集 305 題的實際分布換算）
QUOTA_TABLES = {1: 66, 2: 41, 3: 6, 4: 6, 5: 1}
QUOTA_SHAPE = {"聚合": 60, "純投影/過濾": 31, "聚合+子查詢": 23,
               "+子查詢+否定": 4, "+子查詢": 2}
QUOTA_EXPECT = {"rows": 115, "empty": 3, "schema_unsupported": 2}
QUOTA_WIDE = 27
TOLERANCE = 4       # 每一格允許的偏差

_PUNCT = re.compile(r"[\s，。？：、「」（）()]")


def tri(s):
    s = re.sub(r"\s+", "", s)
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def jac(a, b):
    return len(a & b) / len(a | b) if (a or b) else 0.0


def lcsubstr(a, b):
    """最長共同**連續**子字串。"""
    best, cur = "", [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        nxt = [0] * (len(b) + 1)
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                nxt[j] = cur[j - 1] + 1
                if nxt[j] > len(best):
                    best = a[i - nxt[j]:i]
        cur = nxt
    return best


def tables_of(sql, known):
    return {x.lower() for x in
            re.findall(r"(?:FROM|JOIN)\s+`?([a-zA-Z_][a-zA-Z0-9_]*)`?", sql or "", re.I)
            if x.lower() in known}


def shape_of(sql):
    s = (sql or "").upper()
    k = ("聚合" if re.search(r"\b(SUM|COUNT|AVG|MAX|MIN)\s*\(", s) else "") \
        + ("+視窗" if "OVER" in s else "") \
        + ("+子查詢" if s.count("SELECT") > 1 else "") \
        + ("+否定" if re.search(r"NOT IN|NOT EXISTS", s) else "")
    return k or "純投影/過濾"


def main():
    if not os.path.exists(TESTSET):
        print("找不到 %s" % TESTSET)
        return 1
    raw = io.open(TESTSET, encoding="utf-8").read()
    qs = yaml.safe_load(raw)
    dev = yaml.safe_load(io.open(DEVSET, encoding="utf-8"))
    fail = []

    conn = get_db_manager(MYSQL_URI).engine.connect()
    known = {r[0].lower() for r in conn.execute(text(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE()"))}
    tcm = {t.lower(): (c or "") for t, c in conn.execute(text(
        "SELECT TABLE_NAME, TABLE_COMMENT FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE()"))}
    ccm = {}
    for t, c, m in conn.execute(text(
            "SELECT TABLE_NAME, COLUMN_NAME, COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE()")):
        ccm.setdefault(t.lower(), []).append(m or "")

    # ── [1][2] GT 可執行、expect 相符 ───────────────────────────────
    for e in qs:
        qid, exp = e["id"], str(e.get("expect") or "rows")
        if exp == "schema_unsupported":
            if e.get("sql"):
                fail.append("[2] #%s schema_unsupported 不該有 sql" % qid)
            continue
        try:
            got = list(conn.execute(text(e["sql"])))
        except Exception as ex:
            fail.append("[1] #%s SQL 跑不動：%s %s"
                        % (qid, type(ex).__name__, str(ex)[:90]))
            continue
        if exp == "rows" and not got:
            fail.append("[2] #%s expect rows 但回 0 列" % qid)
        if exp == "empty" and got:
            fail.append("[2] #%s expect empty 但回 %d 列" % (qid, len(got)))

    # ── [3] 不與開發集重複 ──────────────────────────────────────────
    devtri = [(d["id"], tri(d["question"])) for d in dev]
    for e in qs:
        a = tri(e["question"])
        for did, b in devtri:
            if jac(a, b) > DUP_MAX:
                fail.append("[3] #%s 與開發集 #%s 太像（三元組 %.2f > %.2f）"
                            % (e["id"], did, jac(a, b), DUP_MAX))
                break

    # ── [4] 註解洩漏 ────────────────────────────────────────────────
    for e in qs:
        q = _PUNCT.sub("", e["question"])
        worst = ("", "")
        for t in tables_of(e.get("sql") or "", known):
            for cm in [tcm.get(t, "")] + ccm.get(t, []):
                sub = lcsubstr(q, _PUNCT.sub("", cm))
                if len(sub) > len(worst[0]):
                    worst = (sub, t)
        if len(worst[0]) > LEAK_MAX:
            fail.append("[4] #%s 與 %s 的註解重疊 %d 字「%s」> %d —— 換句話問"
                        % (e["id"], worst[1], len(worst[0]), worst[0], LEAK_MAX))

    # ── [5] 分布相符 ────────────────────────────────────────────────
    ct, cs, ce, wide = Counter(), Counter(), Counter(), 0
    for e in qs:
        ce[str(e.get("expect") or "rows")] += 1
        if str(e.get("expect")) == "schema_unsupported":
            continue
        ts = tables_of(e.get("sql") or "", known)
        ct[len(ts)] += 1
        cs[shape_of(e["sql"])] += 1
        if any(t.endswith("_profiles") for t in ts):
            wide += 1
    for label, got, want in (("表數", ct, QUOTA_TABLES), ("形狀", cs, QUOTA_SHAPE),
                             ("expect", ce, QUOTA_EXPECT)):
        for k, v in want.items():
            if abs(got.get(k, 0) - v) > TOLERANCE:
                fail.append("[5] %s %s：%d 題，配額 %d±%d"
                            % (label, k, got.get(k, 0), v, TOLERANCE))
    if abs(wide - QUOTA_WIDE) > TOLERANCE:
        fail.append("[5] 寬表題 %d，配額 %d±%d" % (wide, QUOTA_WIDE, TOLERANCE))

    conn.close()

    def ok(tag):
        return "OK" if not [f for f in fail if f.startswith(tag)] else "✗"

    print("驗收集 %d 題" % len(qs))
    print("[1] GT 可執行　%s" % ok("[1]"))
    print("[2] expect 相符　%s" % ok("[2]"))
    print("[3] 不與開發集重複　%s（三元組上限 %.2f）" % (ok("[3]"), DUP_MAX))
    print("[4] 註解洩漏　%s（最長共同子字串上限 %d 字）" % (ok("[4]"), LEAK_MAX))
    print("[5] 分布　%s" % ok("[5]"))
    print("    表數 %s　寬表 %d／%d" % (dict(sorted(ct.items())), wide, QUOTA_WIDE))
    print("    形狀 %s" % dict(cs.most_common()))
    print("    expect %s" % dict(ce.most_common()))
    if fail:
        print("\n✗ %d 項未過：" % len(fail))
        for f in fail[:40]:
            print("   " + f)
        if len(fail) > 40:
            print("   …還有 %d 項" % (len(fail) - 40))
        print("\n[不通過] 封存前必須全綠 —— 封存之後只能作廢不能修。")
        return 1
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    print("\n[通過] 可以封存。內容 hash = %s" % h)
    print("封存：把這個 hash 寫進 %s，之後每跑一次記一筆"
          "（架構 commit、分數、日期）。" % os.path.basename(RUNLOG))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    log.remove()
    sys.exit(main())
