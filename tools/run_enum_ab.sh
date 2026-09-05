#!/usr/bin/env bash
# enum 最小集的非劣性 A/B（ARCHITECTURE.md §9.12 事前登記）
#
# **交錯跑，不是 A 全跑完再跑 B。** 兩臂各要 6 小時，中間隔著整個
# 服務端的時間窗；A 在前 B 在後的話，托管模型的任何漂移都會被記到 B 頭上
# （[[baselines-die-when-the-model-changes]]，§9.9 的 `#20` −5 就疑似這個）。
# 切成 8 個區塊、每塊 A→B 立刻對照，把漂移的尺度從 6 小時壓到 ~45 分鐘。
#
# 可以隨時 Ctrl-C，再跑一次同一行就接著跑（--out 每題存一次）。
set -u
cd "$(dirname "$0")/.."

PY=.venv/Scripts/python.exe
N=6
OUT=eval/results
A_OUT="$OUT/enum_ab_A.json"
B_OUT="$OUT/enum_ab_B.json"
B_YAML=utils/semantic_layer_enum_min.yaml

export PYTHONIOENCODING=utf-8

# 送出前再驗一次兩臂真的不同 —— 這一步失敗就不准開跑（§9.14 四之六）
$PY tools/verify_enum_arms.py || { echo "!! 兩臂驗證失敗，中止"; exit 1; }

CHUNKS=$($PY - <<'EOF'
import json
ids = sorted(json.load(open("eval/enum_ab_changed.json"))
             + json.load(open("eval/enum_ab_control.json")))
k = 8
size = -(-len(ids) // k)
print("\n".join(",".join(str(q) for q in ids[i:i + size])
                for i in range(0, len(ids), size)))
EOF
)

i=0
while IFS= read -r ids; do
    i=$((i + 1))
    echo "===== 區塊 $i／8：$(echo "$ids" | tr ',' '\n' | wc -l) 題 ×$N ×2 臂 ====="
    echo "--- A 臂（預設）---"
    $PY eval/eval_stability.py --ids "$ids" --n $N --out "$A_OUT" 2>&1 \
        | grep -Ev "loguru|Record was|^Traceback|^  File |^    self\.|^    os\.rename|PermissionError|End of logging"
    echo "--- B 臂（$B_YAML）---"
    SEMANTIC_LAYER_PATH=$B_YAML \
    $PY eval/eval_stability.py --ids "$ids" --n $N --out "$B_OUT" 2>&1 \
        | grep -Ev "loguru|Record was|^Traceback|^  File |^    self\.|^    os\.rename|PermissionError|End of logging"
done <<< "$CHUNKS"

echo "===== 兩臂跑完，對帳："
echo "  $PY tools/score_enum_ab.py"
