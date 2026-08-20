"""
批次評估 Runner
================
跑完整題庫並產出兩份輸出：
  1. eval_report_<ts>.txt   — 人看的逐題報告
  2. eval_result_<ts>.json  — 機器讀的結構化結果，供後續自動稽核

設計重點：
  - 逐題即時寫入 JSON。100 題要跑 3~4 小時，中途中斷不能全部白跑。
  - 支援 --resume：帶入既有的 JSON，跳過已完成的題目接著跑。
  - 明確區分「完成率」與「正確率」。本 Runner 只能判定 Pipeline 有沒有正常產出，
    答案對不對必須另外比對 Ground Truth，不可把完成率當成正確率報告。
"""
import os as _os
import sys as _sys

# 搬進子目錄之後要自己把專案根目錄放進 sys.path（tools/ 也是這個寫法）。
_ROOT = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
if _ROOT not in _sys.path:
    _sys.path.insert(0, _ROOT)
_RESULTS = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "results")

import sys
import os
import json
import time
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from langgraph_sql.graph import compiled_graph

# 最終回答中代表「誠實拒答」的標記（由 final_summarizer 攔截 SCHEMA_UNSUPPORTED 後產生）
_UNSUPPORTED_HINT = "不在資料庫的 schema 與規則定義中"


def classify(result: dict) -> str:
    """
    將單題結果歸類。這是「Pipeline 行為」的分類，不是「答案正確性」的分類。

      crashed             — 執行期例外
      llm_api_error       — LLM API 無回應（逾時 / 429 / 503）。這是基礎設施故障，
                            不是模型能力問題，統計時必須與 error_end 分開。
      error_end           — 重試預算耗盡，完全沒有產出 SQL（最嚴重）
      schema_unsupported  — 觸發防幻覺機制，誠實拒答（這是預期行為，算正常）
      empty               — 有 SQL、執行成功、但查無資料（可能正確也可能錯，需稽核）
      ok                  — 有 SQL 且有資料
    """
    champion_sql = result.get("champion_sql") or ""
    final_answer = result.get("final_answer") or ""

    if not champion_sql:
        # 先判斷是不是 API 掛掉——把網路逾時記成 error_end 會讓評估報告失真。
        if result.get("llm_error"):
            return "llm_api_error"
        return "error_end"
    if _UNSUPPORTED_HINT in final_answer or "SCHEMA_UNSUPPORTED" in champion_sql:
        return "schema_unsupported"
    if not result.get("champion_row_count"):
        return "empty"
    return "ok"


def run_evaluation(file_path: str = _os.path.join(_ROOT, "eval_questions_v2.json"), resume_from: str | None = None):
    if not os.path.exists(file_path):
        print(f"找不到測試檔案 {file_path}")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        questions_data = json.load(f)

    # --- resume：載入既有結果，跳過已完成的題目 ---
    results: list[dict] = []
    done_ids: set = set()
    if resume_from and os.path.exists(resume_from):
        with open(resume_from, "r", encoding="utf-8") as f:
            prior = json.load(f)
        # 因 API 故障失敗的題目要重跑——那不是題目的結果，是網路的結果。
        results = [r for r in prior if r.get("outcome") != "llm_api_error"]
        retry_n = len(prior) - len(results)
        done_ids = {r["id"] for r in results}
        print(f"Resume：沿用 {len(done_ids)} 題既有結果"
              + (f"，重跑 {retry_n} 題 API 故障的題目" if retry_n else ""))

    total_q = len(questions_data)
    _os.makedirs(_RESULTS, exist_ok=True)
    ts = int(time.time())
    report_file = _os.path.join(_RESULTS, f"eval_report_{ts}.txt")
    json_file = resume_from or _os.path.join(
        _RESULTS, f"eval_result_{ts}.json")

    print(f"載入 {total_q} 題；報告 → {report_file}；結構化結果 → {json_file}")

    report = open(report_file, "w", encoding="utf-8")
    report.write("=== Text-to-SQL 評估報告 ===\n")
    report.write(f"題庫: {file_path}\n總題數: {total_q}\n\n")

    try:
        for i, item in enumerate(questions_data):
            q_id = item.get("id", i + 1)
            if q_id in done_ids:
                continue

            q_type = item.get("type", "unknown")
            capability = item.get("capability", "")
            question = item.get("question", "")

            print(f"\n[{i+1}/{total_q}] {q_type} / {capability} | {question}")

            start_time = time.time()
            try:
                state = compiled_graph.invoke({"user_query": question, "retry_count": 0})
                elapsed = time.time() - start_time

                champion_sql = state.get("champion_sql") or ""
                rec = {
                    "id": q_id,
                    "type": q_type,
                    "capability": capability,
                    "question": question,
                    "outcome": classify(state),
                    "elapsed": round(elapsed, 1),
                    "retry": state.get("retry_count", 0),
                    "row_count": state.get("champion_row_count"),
                    "sql_validated": state.get("sql_validated", False),
                    "sql": champion_sql,
                    # 檢索層的決定要留痕，否則一題答錯時分不出是「表沒給對」
                    # 還是「表給對了但 SQL 寫錯」
                    "retrieved_tables": state.get("retrieved_tables") or [],
                    "retrieval_anchors": state.get("retrieval_anchors") or [],
                    # 供稽核：是否用了並列安全的寫法 / 是否用了否定子查詢
                    "used_dense_rank": "DENSE_RANK" in champion_sql.upper(),
                    "used_not_in_exists": ("NOT IN" in champion_sql.upper()
                                           or "NOT EXISTS" in champion_sql.upper()),
                    "final_answer": state.get("final_answer") or "",
                    "error_message": state.get("error_message") or "",
                    "llm_error": state.get("llm_error") or "",
                }
                print(f"  -> {rec['outcome']} | retry={rec['retry']} | "
                      f"rows={rec['row_count']} | {rec['elapsed']}s")
                print(f"  -> SQL: {champion_sql[:100]}" if champion_sql else "  -> 無法生成 SQL")

            except Exception as e:
                elapsed = time.time() - start_time
                rec = {
                    "id": q_id, "type": q_type, "capability": capability,
                    "question": question, "outcome": "crashed",
                    "elapsed": round(elapsed, 1), "exception": f"{type(e).__name__}: {e}",
                }
                print(f"  -> CRASHED: {rec['exception']}")

            results.append(rec)

            report.write(f"【第 {q_id} 題】 ({q_type} / {capability})\n")
            report.write(f"問題: {question}\n")
            report.write(f"結果: {rec['outcome']}\n")
            report.write(f"回答: {rec.get('final_answer', '')}\n")
            report.write(f"SQL: {rec.get('sql', '')}\n")
            report.write(f"筆數: {rec.get('row_count')} | 重試: {rec.get('retry')} | "
                         f"耗時: {rec['elapsed']:.1f}s\n")
            report.write("-" * 60 + "\n")
            report.flush()

            # 逐題落盤，中斷不白跑
            with open(json_file, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
    finally:
        report.close()

    summarize(results, total_q, report_file, json_file)


def summarize(results: list[dict], total_q: int, report_file: str, json_file: str):
    from collections import Counter

    outcomes = Counter(r["outcome"] for r in results)
    n = len(results)

    # 基礎設施故障（API 逾時 / 503）不該計入模型表現，從分母中排除。
    infra = outcomes["llm_api_error"]
    valid = n - infra
    # 「完成」= Pipeline 有正常產出（含誠實拒答與合法空集合）。
    # 這不等於答案正確——正確率必須另外比對 Ground Truth。
    completed = valid - outcomes["error_end"] - outcomes["crashed"]

    print(f"\n{'='*60}\n=== 評估完成（{n}/{total_q} 題）===")
    if infra:
        print(f"⚠️  {infra} 題因 LLM API 無回應被排除（基礎設施故障，非模型問題）")
        print(f"    可用 --resume 在 API 恢復後補跑這些題目")
    if valid:
        print(f"完成率（Pipeline 正常產出）: {completed}/{valid} ({completed/valid*100:.1f}%)")
    print("  註：完成率 ≠ 正確率。答案是否正確需另外比對 Ground Truth。\n")
    for k in ("ok", "empty", "schema_unsupported", "error_end", "llm_api_error", "crashed"):
        if outcomes[k]:
            print(f"  {k:20s}: {outcomes[k]}")

    retries = [r.get("retry", 0) for r in results if "retry" in r]
    if retries:
        print(f"\n重試分布: " + ", ".join(
            f"retry={k} → {v} 題" for k, v in sorted(Counter(retries).items())))

    elapsed = [r["elapsed"] for r in results]
    print(f"耗時: 總計 {sum(elapsed)/3600:.2f} 小時 | 平均 {sum(elapsed)/len(elapsed):.1f}s "
          f"| 最長 {max(elapsed):.1f}s")

    by_type = {}
    for r in results:
        d = by_type.setdefault(r["type"], Counter())
        d[r["outcome"]] += 1
    print("\n依題型:")
    for t, c in by_type.items():
        tot = sum(c.values())
        good = tot - c["error_end"] - c["crashed"]
        print(f"  {t:16s} {good}/{tot} 完成  {dict(c)}")

    bad = [r for r in results if r["outcome"] in ("error_end", "crashed", "llm_api_error")]
    if bad:
        print("\n未產出結果的題目:")
        for r in bad:
            print(f"  #{r['id']} [{r['outcome']}] {r['question'][:44]}")

    print(f"\n報告: {report_file}\n結構化結果: {json_file}")
    # 這支只量「有沒有跑出東西」，答得對不對要對帳。
    # eval_score.py 現在一併做防禦題稽核（§9.1）與僥倖偵測（§9.2）。
    print(f"\n下一步 —— 對帳 + 稽核:\n  python eval/eval_score.py {json_file}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("questions", nargs="?", default=_os.path.join(_ROOT, "eval_questions_v2.json"),
                    help="題庫 JSON 路徑")
    ap.add_argument("--resume", dest="resume", default=None,
                    help="既有的 eval_result_*.json，跳過已完成的題目接著跑")
    args = ap.parse_args()
    run_evaluation(args.questions, args.resume)
