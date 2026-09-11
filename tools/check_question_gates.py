# -*- coding: utf-8 -*-
"""題目可判定性的五道軸 —— 一次掃完，吃任何題庫。

問句要能決定答案，得同時決定五件事。少決定一件，那題就永遠答不對，
量到的是雜訊不是能力：

    [13] 哪幾欄              check_question_shape.py
    [6]  算哪些列            check_flag_ambiguity.py
    [10b] 走快照還是走現算    check_dual_source.py
    [14] 同分誰排前面        check_tie_ambiguity.py
    [15] 怎麼算（去重／空值） check_calc_ambiguity.py

為什麼要有這支入口
================================================================
這五道是分五次、在五次踩雷之後補出來的，每一次都是「事後補的閘門掃出
上一批題的缺陷」。少跑一道的代價很具體：2026-09-11 風格驗證集 30 題
失手，其中 10 題是 [14]／[10b] 的命中，而當時那兩道還不存在。

單獨跑得起來的閘門很容易漏跑。有一個入口就沒有藉口。

⚠️ 這支只讀**問句與 GT SQL**，不讀任何執行結果 —— 所以拿它掃封存的
驗收集不違反協定（跟當初閘門 [6] 掃 v2 是同一件事）。

用法
    python tools/check_question_gates.py eval/testset_holdout.yaml
    python tools/check_question_gates.py            # 預設掃開發集
"""
import os
import subprocess
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

GATES = [
    ("13", "哪幾欄", "check_question_shape.py"),
    ("6", "算哪些列", "check_flag_ambiguity.py"),
    ("10b", "快照還是現算", "check_dual_source.py"),
    ("14", "同分誰排前面", "check_tie_ambiguity.py"),
    ("15", "怎麼算", "check_calc_ambiguity.py"),
]


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    path = args[0] if args else "eval_ground_truth.yaml"
    verbose = "-v" in sys.argv or "--verbose" in sys.argv

    print("題庫：%s\n" % path)
    bad = 0
    for num, what, script in GATES:
        r = subprocess.run(
            [sys.executable, os.path.join(_ROOT, "tools", script), path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=_ROOT, env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        out = (r.stdout or "") + (r.stderr or "")
        lines = [ln for ln in out.split("\n")
                 if ln.strip().startswith("#") or ln.startswith("命中")]
        mark = "OK" if r.returncode == 0 else "✗"
        hit = next((ln for ln in lines if ln.startswith("命中")), "")
        print("[%-3s] %-14s %s %s" % (num, what, mark, hit))
        if r.returncode != 0:
            bad += 1
            for ln in lines:
                if ln.strip().startswith("#"):
                    print("        %s" % ln.strip())
        if verbose:
            print("\n".join("        " + ln for ln in out.split("\n") if ln.strip()))

    print()
    if bad:
        print("✗ %d/%d 道紅燈 —— 問句決定不了答案的題，修問句不修 GT。" % (bad, len(GATES)))
    else:
        print("五道全綠。注意 [10b] 只掃「GT 走寬表」那個方向，反方向仍然沒有偵測器。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
