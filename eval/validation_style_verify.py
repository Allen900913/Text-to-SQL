# -*- coding: utf-8 -*-
"""風格驗證集的閘門 —— 核心是「風格與配額也要可檢查」。

第一組驗證集（validation_shapes.yaml）的失敗是我自己講了「照形狀出」
就真的只管形狀，沒有任何機制檢查它像不像驗收集。結果它的問句長一倍、
GT 列數多 4.4 倍，跑出 97.3% —— 量不到東西。

所以這一組把「像不像」寫成閘門：

    [1] GT 可執行；expect rows 要非空、expect empty 要真的空
    [2] 列數上限（太大就只是在測 LIMIT）
    [3] 不與開發集／驗收集／驗證集一重複（字元三元組 Jaccard）
    [4] 題目可判定性五道軸                      （tools/check_question_gates.py）
        [13] 哪幾欄 / [6] 算哪些列 / [10b] 快照還是現算 /
        [14] 同分誰排前面 / [15] 怎麼算
    [5]（併入 [4]）
    [6] **風格**：問句長度中位數要貼齊驗收集
    [7] **配額**：表數／形狀／寬窄／expect 的分布要貼齊驗收集（照題數比例縮放）

[6][7] 是這一組存在的理由。沒有它們，這支跟第一組會犯一樣的錯。

用法：
    python eval/validation_style_verify.py
"""
import io
import os
import subprocess
import sys
from collections import Counter

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for p in (_ROOT, os.path.dirname(os.path.abspath(__file__))):
    if p not in sys.path:
        sys.path.insert(0, p)

import yaml
from sqlalchemy import text

from eval_score import to_rows  # noqa: F401  （確保判分器與這支同源）
from langgraph_sql.config import MYSQL_URI
from langgraph_sql.utils.db_manager import get_db_manager
from testset_verify import shape_of, tables_of

PATH = os.path.join(_ROOT, "eval", "validation_style.yaml")
HOLDOUT = os.path.join(_ROOT, "eval", "testset_holdout.yaml")
OTHERS = (os.path.join(_ROOT, "eval_ground_truth.yaml"), HOLDOUT,
          os.path.join(_ROOT, "eval", "validation_shapes.yaml"))
ROW_MAX = 220
DUP_MAX = 0.62
LEN_TOL = 4         # 問句長度中位數容許差幾個字
QUOTA_TOL = 4       # 每一格容許偏差幾題（與 testset_verify 同）


def tri(s):
    s = "".join(s.split())
    return {s[i:i + 3] for i in range(max(0, len(s) - 2))}


def jaccard(a, b):
    return len(a & b) / len(a | b) if a | b else 0.0


def profile(entries, known):
    """(表數, 形狀, expect, 寬表數) —— 與 testset_verify [5] 同一套算法。"""
    ct, cs, ce, wide = Counter(), Counter(), Counter(), 0
    for e in entries:
        ce[str(e.get("expect") or "rows")] += 1
        if str(e.get("expect")) == "schema_unsupported":
            continue
        ts = tables_of(e.get("sql") or "", known)
        ct[len(ts)] += 1
        cs[shape_of(e["sql"])] += 1
        if any(t.endswith("_profiles") for t in ts):
            wide += 1
    return ct, cs, ce, wide


def gate(script, path):
    """跑既有的閘門腳本，回傳 (通過?, 輸出)。"""
    r = subprocess.run([sys.executable, os.path.join(_ROOT, "tools", script), path],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", cwd=_ROOT,
                       env=dict(os.environ, PYTHONIOENCODING="utf-8"))
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def main():
    entries = yaml.safe_load(io.open(PATH, encoding="utf-8"))
    hold = yaml.safe_load(io.open(HOLDOUT, encoding="utf-8"))
    db = get_db_manager(MYSQL_URI)
    conn = db.engine.connect()
    known = {r[0].lower() for r in conn.execute(text(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_SCHEMA = DATABASE()"))}
    bad = {k: [] for k in range(1, 8)}

    print("風格驗證集 %d 題（驗收集 %d 題）\n" % (len(entries), len(hold)))

    others = []
    for p in OTHERS:
        if os.path.exists(p):
            for e in yaml.safe_load(io.open(p, encoding="utf-8")) or []:
                others.append((e["id"], tri(e["question"])))

    nrows = {}
    for e in entries:
        qid, sql = e["id"], e.get("sql")
        want = str(e.get("expect") or "rows")
        if sql:
            try:
                df = db.execute_to_dataframe(sql)
            except Exception as exc:
                bad[1].append((qid, "%s: %s" % (type(exc).__name__, exc)))
                continue
            n = len(df)
            nrows[qid] = n
            if want == "rows" and n == 0:
                bad[1].append((qid, "expect rows 卻是空集合"))
            if want == "empty" and n != 0:
                bad[1].append((qid, "expect empty 卻有 %d 列" % n))
            if n > ROW_MAX:
                bad[2].append((qid, "%d 列 > %d" % (n, ROW_MAX)))
        elif want != "schema_unsupported":
            bad[1].append((qid, "沒有 sql，而 expect 不是 schema_unsupported"))

        t = tri(e["question"])
        hi = max(((jaccard(t, o), oid) for oid, o in others), default=(0.0, None))
        if hi[0] > DUP_MAX:
            bad[3].append((qid, "與 #%s 相似 %.2f" % (hi[1], hi[0])))

    # 五道軸走同一個入口 —— 單獨跑得起來的閘門很容易漏跑，而漏跑的代價
    # 就是第一版這組題的 [14]／[10b] 共 9 題全部溜過去。
    okg, outg = gate("check_question_gates.py", PATH)
    if not okg:
        for line in outg.split("\n"):
            t = line.strip()
            if t.startswith("[") and "✗" in t:
                bad[4].append(("-", t))
            elif t.startswith("#"):
                bad[4].append((t.split()[0].lstrip("#"), t))

    # [6] 風格
    ql = sorted(len(e["question"]) for e in entries)
    hl = sorted(len(e["question"]) for e in hold)
    med, hmed = ql[len(ql) // 2], hl[len(hl) // 2]
    if abs(med - hmed) > LEN_TOL:
        bad[6].append(("-", "問句長度中位 %d，驗收集 %d（容許 ±%d）" % (med, hmed, LEN_TOL)))

    # [7] 配額（照題數比例縮放驗收集）
    ct, cs, ce, wide = profile(entries, known)
    hct, hcs, hce, hwide = profile(hold, known)
    k = len(entries) / len(hold)
    for label, got, want in (("表數", ct, hct), ("形狀", cs, hcs), ("expect", ce, hce)):
        for key, v in want.items():
            tgt = round(v * k)
            if abs(got.get(key, 0) - tgt) > QUOTA_TOL:
                bad[7].append(("-", "%s %s：%d 題，配額 %d±%d"
                               % (label, key, got.get(key, 0), tgt, QUOTA_TOL)))
    if abs(wide - round(hwide * k)) > QUOTA_TOL:
        bad[7].append(("-", "寬表題 %d，配額 %d±%d" % (wide, round(hwide * k), QUOTA_TOL)))

    names = {
        1: "GT 可執行且 expect 相符",
        2: "列數 <= %d" % ROW_MAX,
        3: "不與既有三組題重複",
        4: "題目可判定性五道軸（[13][6][10b][14][15]）",
        5: "（併入 [4]）",
        6: "風格：問句長度貼齊驗收集",
        7: "配額：表數／形狀／寬窄／expect",
    }
    fail = 0
    for i in sorted(names):
        n = len(bad[i])
        fail += n
        print("[%d] %-32s %s" % (i, names[i], "OK" if not n else "✗ %d 項" % n))
        for qid, why in bad[i]:
            print("      %s  %s" % (qid, why))

    print("\n問句長度　中位 %d／驗收集 %d　平均 %.1f" % (med, hmed, sum(ql) / len(ql)))
    print("表數 %s　寬表 %d／配額 %d" % (dict(sorted(ct.items())), wide, round(hwide * k)))
    print("形狀 %s" % dict(cs.most_common()))
    print("expect %s" % dict(ce.most_common()))
    if nrows:
        v = sorted(nrows.values())
        print("GT 列數　中位 %d　最大 %d（驗收集中位 5）" % (v[len(v) // 2], v[-1]))
    print("\n%s" % ("全綠" if not fail else "✗ 共 %d 項待修" % fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
