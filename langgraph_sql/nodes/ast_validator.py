"""
Node 3: AST Validator
======================
使用 sqlglot 進行確定性快篩（MySQL 方言）。

三層過濾：
  1. 語法解析 — sqlglot.parse_one(sql, read="mysql")
  2. 安全過濾 — Root 必須是 SELECT / UNION
  3. 幻覺過濾 — 檢查 Table 與 Column 是否存在於 Schema（含 Alias 解析，fail-open）

通過後對最外層查詢強制注入或收斂為 LIMIT 500，避免結果集過大。
"""
import os

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from loguru import logger as log

from langgraph_sql.state import AgentState
from langgraph_sql.utils.schema_registry import get_allowed_tables, get_table_columns

# 第 3d 層「越界引用」的處置方式。**預設 union。**
#
#   union （預設）不淘汰。把那張表**聯集進 scope**、補上它的 DDL，
#                 並記進 `scope_extra` 供檢索指標校正。
#   strict        淘汰這條 SQL，補 DDL 後重試（2026-09-05 當天的實作）。
#   off           完全不檢查、也不記錄（2026-09-05 之前的行為）。
#
# 為什麼預設不是 strict —— 見下方 3d 區塊的長註解。一句話：
# **檢索範圍是機器猜的、已知會漏，不該拿安全層的嚴格度去執行它。**
#     SCOPE_MODE=strict python eval/test_runner.py
SCOPE_MODE = os.environ.get("SCOPE_MODE", "union").lower()
if os.environ.get("SCOPE_STRICT") == "0":      # 舊開關，保留相容
    SCOPE_MODE = "off"


# ===========================================================================
# 輔助函數
# ===========================================================================

def _build_alias_map(ast) -> dict[str, str]:
    """
    遍歷 AST 中所有 Table 節點，建立 Alias → Table Name 映射。
    例如：FROM customers c → {"c": "customers", "customers": "customers"}
    """
    alias_map: dict[str, str] = {}
    for table_node in ast.find_all(exp.Table):
        real_name = table_node.name.lower()
        alias = (table_node.alias or "").lower()
        if alias:
            alias_map[alias] = real_name
        alias_map[real_name] = real_name
    return alias_map


def _collect_cte_names(ast) -> set[str]:
    """
    收集 CTE (WITH) 中定義的名稱。
    CTE 名稱在 FROM 中使用時不應被當作「不存在的表」。
    """
    cte_names: set[str] = set()
    for cte_node in ast.find_all(exp.CTE):
        if cte_node.alias:
            cte_names.add(cte_node.alias.lower())
    return cte_names


def _detect_negation_antipatterns(ast) -> list[str]:
    """
    偵測「否定存在量詞」反模式：LEFT JOIN 搭配 WHERE 中的 <> / !=。

    「從未買過 X」這類問題若寫成
        LEFT JOIN products p ... WHERE p.name <> 'X'
    會把「買過其他商品」的訂單也撈進來，導致真正買過 X 的人依然出現在結果中。
    正確做法是 NOT IN / NOT EXISTS 子查詢。

    判斷條件刻意收斂到「同一層 SELECT 內同時具備 LEFT JOIN 與 WHERE 的不等式」：
      - INNER JOIN + <>（例如排除 CANCELLED 訂單）是合法過濾，不攔。
      - <> 寫在 JOIN ON 內（例如自連接排除自己）是合法用法，不攔。
      - LEFT JOIN + IS NULL（「從未下過任何訂單」）是正確寫法，不攔。

    回傳所有命中的淘汰理由；若無此反模式則回傳空列表。
    """
    offenders: list[str] = []

    for select in ast.find_all(exp.Select):
        joins = select.args.get("joins") or []
        if not any((j.side or "").upper() == "LEFT" for j in joins):
            continue

        where = select.args.get("where")
        if where is None:
            continue

        for neq in where.find_all(exp.NEQ):
            expr = neq.sql(dialect="mysql")
            if expr not in offenders:
                offenders.append(expr)

    if not offenders:
        return []

    return [
        f"偵測到否定存在量詞的反模式：LEFT JOIN 搭配 WHERE 中的不等式 "
        f"{'、'.join(f'`{o}`' for o in offenders)}。這種寫法會讓「買過其他商品」的"
        f"紀錄通過篩選，導致實際買過該商品的對象仍出現在結果中。"
        f"請改用 NOT IN 或 NOT EXISTS 子查詢排除「曾經符合條件」的對象，"
        f"例如：WHERE c.id NOT IN (SELECT o.customer_id FROM orders o "
        f"JOIN order_items oi ON o.id = oi.order_id "
        f"JOIN products p ON oi.product_id = p.id WHERE p.name = '商品名')。"
    ]


def _detect_limit1_truncation(ast) -> list[str]:
    """
    偵測「極值截斷」反模式：最外層 ORDER BY 搭配 LIMIT 1。

    問「最多 / 最高 / 哪一天最…」時若用 ORDER BY ... LIMIT 1，遇到並列 (Ties)
    會任意保留一筆、丟掉其餘同分者，答案看起來合理但其實不完整。
    正確做法是 DENSE_RANK() OVER (ORDER BY ... DESC) 後過濾 rnk = 1。

    判斷條件刻意只看「最外層」查詢：
      - 純 LIMIT 1 但沒有 ORDER BY（例如「隨便給我一筆」）不攔。
      - 子查詢中的 ORDER BY ... LIMIT 1 不攔：像
        `WHERE amount = (SELECT amount FROM orders ORDER BY amount DESC LIMIT 1)`
        取的是「那個數值」，外層仍會比對出所有並列的列，沒有截斷問題。
      - LIMIT N (N > 1) 不攔，那是使用者明確要的 Top-N。
    """
    limit_node = ast.args.get("limit")
    order_node = ast.args.get("order")
    if limit_node is None or order_node is None:
        return []

    expr = limit_node.expression
    if not (isinstance(expr, exp.Literal) and expr.is_int and int(expr.this) == 1):
        return []

    return [
        "偵測到極值截斷的反模式：最外層使用 ORDER BY ... LIMIT 1。"
        "若有多筆並列第一（同樣的最大值），LIMIT 1 只會任意留下一筆，"
        "其餘同分者會被無聲丟棄，導致答案不完整。"
        "請改用 DENSE_RANK() 取出所有並列第一，例如："
        "WITH ranked AS (SELECT <欄位>, <聚合值> AS metric, "
        "DENSE_RANK() OVER (ORDER BY <聚合值> DESC) AS rnk "
        "FROM ... GROUP BY ...) "
        "SELECT <欄位>, metric FROM ranked WHERE rnk = 1;"
    ]


def _inject_limit(ast, limit: int = 500) -> str:
    """
    對最外層 SELECT / UNION 加上 LIMIT，並把過大的常數 LIMIT 收斂到上限。
    不影響原本較小的 LIMIT。
    """
    outer_limit = ast.args.get("limit")
    if outer_limit is None:
        ast = ast.limit(limit)
    else:
        limit_expression = outer_limit.expression
        if isinstance(limit_expression, exp.Literal) and limit_expression.is_int:
            if int(limit_expression.this) > limit:
                outer_limit.set("expression", exp.Literal.number(limit))

    return ast.sql(dialect="mysql")


# ===========================================================================
# Node 主函數
# ===========================================================================

def ast_validator(state: AgentState) -> dict:
    """使用 sqlglot 進行 AST 層級的 SQL 快篩。"""
    candidates = state.get("candidate_sqls", [])
    log.info(f"[Node 3] AST Validator — 檢查 {len(candidates)} 條候選 SQL")

    if not candidates:
        log.warning("[Node 3] 無候選 SQL 可檢查")
        retry = state.get("retry_count", 0) + 1
        return {
            "valid_sqls": [],
            "retry_count": retry,
            "db_error": "上一輪未產生任何候選 SQL，請重新生成一條有效的 MySQL SELECT 查詢。",
            "error_message": "所有候選 SQL 均解析失敗，無法進行驗證。",
        }

    # 白名單來自 MySQL 的 INFORMATION_SCHEMA（見 schema_registry 的說明），
    # 不再依賴 regex 剖析 YAML DDL —— 那條路徑失效時是靜默的。
    allowed_tables: set[str] = get_allowed_tables()
    table_columns: dict[str, list[str]] = get_table_columns()

    # 這一題實際拿到 DDL 的表 = 檢索層給的 ＋ 先前輪次補進來的（見 3d）。
    # 檢索失效時 context_retriever 會回傳全部的表，那時 scope 涵蓋全庫、3d 不會觸發。
    scope: set[str] = {t.lower() for t in (state.get("retrieved_tables") or [])}
    scope |= {t.lower() for t in (state.get("scope_extra") or [])}
    if SCOPE_MODE == "off" or scope >= allowed_tables:
        scope = set()          # 關閉、或沒有剪裁 → 不做越界檢查
    scope_escapes: set[str] = set()

    valid_sqls: list[str] = []
    # 收集每條 SQL 的淘汰理由，供全部淘汰時回填 db_error 給 Generator 自我修復。
    rejection_reasons: list[str] = []

    for i, sql in enumerate(candidates):
        tag = f"SQL #{i+1}"
        try:
            # ============================================================
            # 第一層：語法解析
            # ============================================================
            ast = sqlglot.parse_one(sql, read="mysql")

            # ============================================================
            # 第二層：安全過濾（Root 必須是 SELECT / UNION）
            # ============================================================
            if not isinstance(ast, (exp.Select, exp.Union)):
                reason = (f"Root 非 SELECT/UNION (type={type(ast).__name__})；"
                          "只允許 SELECT 或 WITH (CTE) 查詢。")
                log.debug(f"  {tag} 淘汰 — {reason}")
                rejection_reasons.append(f"{tag}: {reason}")
                continue

            # ============================================================
            # 第三層：幻覺過濾
            # ============================================================
            alias_map = _build_alias_map(ast)
            cte_names = _collect_cte_names(ast)

            # 第三層採「一次性體檢」：3a/3b/3c 全部掃完再結算，讓 Generator
            # 一輪就看到所有毛病。逐項 break 會讓每次重試只修掉一個錯，
            # 在 MAX_RETRIES 很小的情況下必定燒光預算。
            issues: list[str] = []

            # --- 3a. 檢查 Table 是否存在於 Schema ---
            bad_tables: list[str] = []
            for table_node in ast.find_all(exp.Table):
                tname = table_node.name.lower()
                if tname in cte_names:
                    continue  # CTE 定義的名稱，不需要在 Schema 中
                if tname not in allowed_tables and tname not in bad_tables:
                    bad_tables.append(tname)

            if bad_tables:
                issues.append(
                    f"不存在的表: {', '.join(bad_tables)}"
                    f"（合法的表: {', '.join(sorted(allowed_tables))}）"
                )

            # --- 3b. 檢查 Column 是否存在（fail-open 策略） ---
            #   - 有明確 table 引用的 Column → 解析 Alias 後比對
            #   - 無 table 引用的 Column（如 SELECT name）→ 放行
            #   - 無法解析歸屬的 Column（如聚合函數內）→ 放行
            #   同一張表的幻覺欄位會合併成一則訊息，避免合法欄位清單重複列印。
            bad_columns: dict[str, list[str]] = {}
            for col_node in ast.find_all(exp.Column):
                col_name = col_node.name.lower()
                table_ref = (col_node.table or "").lower()

                if col_name == "*":
                    # `SELECT p.*` 會被 sqlglot 解析成 Column(name='*', table='p')，
                    # 拿 '*' 去比對欄位清單必然落空 → 對**合法 SQL** 判成幻覺欄位。
                    # 實測 `#17` 因此燒光重試預算變成 error_end（連答案都沒有）。
                    # 注意只有**帶表限定**的星號會這樣：`SELECT *` 與 `COUNT(*)`
                    # 解析出來是 exp.Star，根本不會進到這個迴圈。
                    continue

                if not table_ref:
                    continue  # 無表引用 → 放行

                # 解析 Alias → 真實 Table Name
                real_table = alias_map.get(table_ref)
                if real_table is None:
                    continue  # 無法解析（可能是子查詢 alias）→ 放行

                known_cols = table_columns.get(real_table)
                if known_cols is None:
                    continue  # 表不在 Schema 映射中 → 放行

                if col_name not in known_cols:
                    seen = bad_columns.setdefault(real_table, [])
                    if col_name not in seen:
                        seen.append(col_name)

            for real_table, cols in bad_columns.items():
                issues.append(
                    f"{real_table} 不存在的欄位: {', '.join(cols)}"
                    f"（{real_table} 的合法欄位: "
                    f"{', '.join(table_columns[real_table])}）"
                )

            # --- 3c. 業務反模式黑名單（確定性攔截，不依賴模型自覺） ---
            issues.extend(_detect_negation_antipatterns(ast))
            issues.extend(_detect_limit1_truncation(ast))

            # --- 3d. 越界引用：表真的存在，但這一題沒給過它的 DDL ---
            #
            # 為什麼要擋（2026-09-05，ARCHITECTURE §9.11／§9.13）：
            # `context_retriever._other_tables_line()` 會列出沒被選中的表名並說
            # 「需要時可直接使用」，而 24 則 few-shot 反覆示範 orders／customers／
            # products 的欄位。兩者相加的結果是**模型可以靠記憶寫一張它沒拿到
            # 欄位定義的表**。六輪 × 兩臂 1,823 次取樣裡出現 20 次，20 次全對 ——
            # 所以這不是幻覺（3a 擋掉不存在的表、3b 擋掉不存在的欄位），
            # 而是「用了沒看過的表」。它有兩個真傷害：
            #
            #   ① **檢索範圍不再是邊界**，檢索指標因此失真。§9.11 量到欄位提示
            #      的召回比對照低 1.0pp、e2e 卻高 1.4pp，差額全部走這條路。
            #   ② **模型依賴的是 few-shot 記住的 schema，那份記憶會過期。**
            #      欄位改名時 few-shot 還在教舊名字，而這條路徑不會報錯。
            #
            # 處置是**聯集，不是淘汰**（2026-09-05 改；理由見 §9.13 補記）。
            # 上面 ② 那條理由後來查證是錯的：3b 是拿真實 INFORMATION_SCHEMA 比對，
            # 欄位改名時 `orders.total_price` 會被 3b 明確擋下並列出合法欄位 ——
            # **schema drift 早就擋住了**，3d 沒有多買到這一項。
            # 剩下真正站得住的只有 ①「檢索指標失真」，而那**只需要記錄，不需要攔截**。
            #
            # 攔截的代價則是實的三項：
            #   ‧ `MAX_RETRIES = 2` 是跨節點共用的總預算，一次攔截吃掉一半；
            #   ‧ `executor_voter` 是對 `valid_sqls` 投票，淘汰等於改變投票結果 ——
            #     實測那 20 次越界的 SQL **全是對的**，攔掉是把對的踢出候選；
            #   ‧ 檢索範圍是**機器猜的**（實測召回 98.4–99.3%，已知會漏）。
            #     拿安全層（3a 全庫白名單）的嚴格度去執行一個會漏的猜測，
            #     等於把「檢索的錯」升級成「生成的死路」。
            #
            # 所以這裡只做兩件事：把那張表**聯集進 scope**（下面補 DDL），
            # 並記進 `scope_extra` 讓檢索指標算得回來。SQL 本身照常通過 ——
            # 它已經過了 3a（表存在）與 3b（欄位存在）。
            # 這與 RSL-SQL 的 backward pruning 是同一個 union，差別只在
            # **它對每一題先付一次全庫 call 去製造訊號，這裡等模型真的伸手才付**
            # （全庫 DDL 77,731 字元 vs 剪裁後平均 2,186，35.6 倍；觸發率 20/1823）。
            if scope:
                escaped = sorted({
                    t.name.lower() for t in ast.find_all(exp.Table)
                    if t.name.lower() not in cte_names
                    and t.name.lower() in allowed_tables
                    and t.name.lower() not in scope
                })
                if escaped:
                    scope_escapes.update(escaped)
                    if SCOPE_MODE == "strict":
                        issues.append(
                            f"用了沒有提供欄位定義的表: {', '.join(escaped)}"
                            "（這些表確實存在，但上面的 schema 沒有列出它們的欄位，"
                            "不可以憑印象寫。已在下方補上它們的定義，請據此改寫）"
                        )

            # --- 結算：一次列出所有問題 ---
            if issues:
                detail = "；".join(
                    f"({n}) {issue}" for n, issue in enumerate(issues, 1)
                )
                log.debug(f"  {tag} 淘汰 — 共 {len(issues)} 項問題: {detail}")
                rejection_reasons.append(f"{tag} 共 {len(issues)} 項問題：{detail}")
                continue

            # ============================================================
            # 全部通過 → 注入最外層 LIMIT 500 後再轉回 SQL 字串
            # ============================================================
            final_sql = _inject_limit(ast, limit=500)
            valid_sqls.append(final_sql)
            log.debug(f"  {tag} ✅ 通過 → {final_sql[:120]}...")

        except ParseError as e:
            reason = f"語法錯誤: {e}"
            log.debug(f"  {tag} 淘汰 — {reason}")
            rejection_reasons.append(f"{tag}: {reason}")
            continue
        except Exception as e:
            reason = f"未預期錯誤: {type(e).__name__}: {e}"
            log.debug(f"  {tag} 淘汰 — {reason}")
            rejection_reasons.append(f"{tag}: {reason}")
            continue

    log.info(f"[Node 3] 通過快篩: {len(valid_sqls)}/{len(candidates)}")

    result: dict = {"valid_sqls": valid_sqls}

    # 若全部淘汰，遞增 retry_count，並把淘汰理由回填 db_error。
    # db_error 是 sql_generator 唯一會讀進修復 Prompt 的欄位；只設 error_message
    # 會讓模型在沒有任何提示的情況下重生成，temperature=0 幾乎必然產出同一條 SQL。
    # 越界引用（3d）→ 聯集進 scope。
    #
    # `scope_extra` **無條件記錄**：它是這一層唯一站得住的收益（檢索指標校正），
    # 而且要在 SQL 通過時也記得到 —— union 模式下通過才是常態。
    # 刻意**不寫回 `retrieved_tables`**：那一欄是檢索層的輸出，混進來的話
    # 「檢索漏了、生成端救回來」會被記成檢索沒漏（§9.11）。
    #
    # DDL 則是補給**之後**的輪次用的（db_validator／executor 失敗而重生成時），
    # 讓模型從「靠 few-shot 的記憶猜」變成「被明確告知」。補過之後那張表就在
    # scope 裡，不會再次觸發，所以不會迴圈。
    if scope_escapes:
        from langgraph_sql.utils.schema_parser import get_schema_parser
        result["scope_extra"] = sorted(
            set(state.get("scope_extra") or []) | scope_escapes)
        try:
            result["schema_ddl"] = (
                state.get("schema_ddl", "")
                + "\n-- 你上一輪用到、但先前沒有列出的表（現在補上）:\n"
                + get_schema_parser().get_ddl_for(set(scope) | scope_escapes)
            )
            log.warning(
                f"[Node 3] 越界引用 {sorted(scope_escapes)} —— 已聯集進 scope"
                f"（模式={SCOPE_MODE}）。檢索層漏了這些表，"
                f"retrieved_tables 保持不動，見 §9.11"
            )
        except Exception as e:      # 補 DDL 失敗不該讓整題死掉
            log.warning(f"[Node 3] 補越界表 DDL 失敗（{type(e).__name__}: {e}），照常繼續")

    if not valid_sqls:
        result["retry_count"] = state.get("retry_count", 0) + 1
        detail = "\n".join(rejection_reasons)
        result["db_error"] = detail or "所有候選 SQL 均未通過 AST 驗證。"
        result["error_message"] = f"所有候選 SQL 均未通過 AST 驗證。{detail}"
        log.warning(
            f"[Node 3] 全部淘汰，retry_count={result['retry_count']}；"
            f"理由: {detail[:200]}"
        )
    else:
        # 有 SQL 通過時清掉上一輪的錯誤，避免舊訊息殘留到下個節點。
        result["db_error"] = ""

    return result
