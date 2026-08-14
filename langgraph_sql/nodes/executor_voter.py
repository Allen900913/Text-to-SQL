"""
Node 4: Executor & Voter
==========================
在沙盒中執行通過 AST 快篩的 SQL，進行 DataFrame 排序 Hash 投票。

關鍵修正：
  - ✅ 空集合保留（不丟棄），參與投票
  - ✅ Hash 前排序 + reset_index，消除行順序差異
  - ✅ MySQL max_execution_time 超時防護
  - ✅ AST 層已注入 LIMIT 500，正常情況下不會因結果集過大而丟棄
"""
import hashlib
import json
import pandas as pd
from loguru import logger as log

from langgraph_sql.state import AgentState
from langgraph_sql.config import MYSQL_URI, SQL_TIMEOUT_MS, MAX_RESULT_ROWS
from langgraph_sql.utils.db_manager import get_db_manager


def _dataframe_to_json(df: pd.DataFrame) -> str:
    """
    將 DataFrame 轉為 JSON 字串。
    處理 datetime、Decimal 等不可序列化的型別。
    """
    if df.empty:
        return "[]"

    records = df.to_dict(orient="records")
    for row in records:
        for k, v in row.items():
            # datetime → ISO 格式字串
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()
            # numpy 數值型別 → Python 原生型別
            elif hasattr(v, "item"):
                row[k] = v.item()
            # Decimal → float
            elif hasattr(v, "__float__") and not isinstance(v, (int, float)):
                row[k] = float(v)
            # NaN → None
            elif isinstance(v, float) and pd.isna(v):
                row[k] = None

    return json.dumps(records, ensure_ascii=False, indent=2, default=str)


def _hash_dataframe(df: pd.DataFrame) -> str:
    """
    對 DataFrame 進行排序後 Hash。
    排序消除行順序差異，確保語義等價的 SQL 產生相同 Hash。
    """
    if df.empty:
        # 空 DataFrame：所有空結果都應該 Hash 相同
        return hashlib.md5(b"__EMPTY_RESULT__").hexdigest()

    # 排序：按所有 Column 排序，消除行順序差異
    try:
        df_sorted = df.sort_values(
            by=df.columns.tolist()
        ).reset_index(drop=True)
    except TypeError:
        # 若有不可排序的型別（罕見），使用原始順序
        df_sorted = df.reset_index(drop=True)

    # Hash：使用 pandas 內建的 hash 函數
    hash_values = pd.util.hash_pandas_object(df_sorted).values.tobytes()
    return hashlib.md5(hash_values).hexdigest()


# ===========================================================================
# Node 主函數
# ===========================================================================

def executor_voter(state: AgentState) -> dict:
    """執行通過快篩的 SQL，用 Hash 投票選出冠軍。"""
    valid_sqls = state.get("valid_sqls", [])
    log.info(f"[Node 4] Executor & Voter — 執行 {len(valid_sqls)} 條 SQL")

    if not valid_sqls:
        return {
            "champion_sql": "",
            "champion_result": "",
            "error_message": "沒有通過驗證的 SQL 可以執行。",
        }

    db = get_db_manager(MYSQL_URI)

    # 投票相關的資料結構
    vote_counts: dict[str, int] = {}          # hash → 票數
    hash_to_sql: dict[str, str] = {}          # hash → 第一條對應 SQL
    hash_to_result: dict[str, str] = {}       # hash → JSON 結果字串
    hash_to_rowcount: dict[str, int] = {}     # hash → 結果筆數（權威值，供 Summarizer 引用）
    execution_errors: list[str] = []          # 執行失敗的錯誤訊息

    for i, sql in enumerate(valid_sqls):
        tag = f"SQL #{i+1}"
        try:
            # 執行 SQL（含超時防護）
            df = db.execute_to_dataframe(sql, timeout_ms=SQL_TIMEOUT_MS)

            # 防禦性檢查：正常由 AST 的 LIMIT 500 保證不會進入此分支。
            if len(df) > MAX_RESULT_ROWS:
                error = (
                    f"結果集超過上限 ({len(df)} rows > {MAX_RESULT_ROWS})；"
                    "請在 SQL 最外層加入較小的 LIMIT。"
                )
                execution_errors.append(f"{tag}: {error}")
                log.warning(f"  {tag} ❌ {error}")
                continue

            # 排序後 Hash
            result_hash = _hash_dataframe(df)

            # 投票
            vote_counts[result_hash] = vote_counts.get(result_hash, 0) + 1

            # 記錄第一條對應的 SQL 和結果
            if result_hash not in hash_to_sql:
                hash_to_sql[result_hash] = sql
                hash_to_result[result_hash] = _dataframe_to_json(df)
                hash_to_rowcount[result_hash] = len(df)

            log.debug(
                f"  {tag} ✅ 執行成功 "
                f"({len(df)} rows, hash={result_hash[:8]}...)"
            )

        except Exception as e:
            err_msg = str(e)
            execution_errors.append(f"{tag}: {err_msg}")
            log.debug(f"  {tag} ❌ 執行失敗: {err_msg[:120]}")
            continue

    # ------------------------------------------------------------------
    # 投票結果
    # ------------------------------------------------------------------
    if not vote_counts:
        error_detail = "; ".join(execution_errors[:3])
        log.warning(f"[Node 4] 所有 SQL 執行皆失敗: {error_detail}")
        retry = state.get("retry_count", 0) + 1
        return {
            "champion_sql": "",
            "champion_result": "",
            "db_error": error_detail or "SQL 執行未產生可用結果。",
            "retry_count": retry,
            "error_message": f"所有 SQL 執行皆失敗。代表性錯誤: {error_detail}",
        }

    # Majority Voting：找出最高票
    champion_hash = max(vote_counts, key=vote_counts.get)
    vote = vote_counts[champion_hash]
    total_success = sum(vote_counts.values())
    champion_sql = hash_to_sql[champion_hash]
    champion_result = hash_to_result[champion_hash]
    champion_row_count = hash_to_rowcount[champion_hash]

    # 投票統計 Log
    log.info(
        f"[Node 4] 投票結果: "
        f"{len(vote_counts)} 組不同結果, "
        f"冠軍票數 = {vote}/{total_success}, "
        f"{champion_row_count} rows, "
        f"hash = {champion_hash[:8]}..."
    )
    log.debug(f"[Node 4] 冠軍 SQL: {champion_sql}")
    log.debug(f"[Node 4] 冠軍結果預覽: {champion_result[:200]}...")

    return {
        "champion_sql": champion_sql,
        "champion_result": champion_result,
        "champion_row_count": champion_row_count,
        "db_error": "",
        "error_message": "",
    }
