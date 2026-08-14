# LangGraph Text-to-SQL Pipeline

本目錄是目前使用中的 LangGraph 版 Text-to-SQL 流程。

核心原則：
- 不使用 LLM 語意審稿節點。
- SQL 可執行性由 MySQL 決定。
- 失敗時以資料庫原生錯誤驅動 SQL 修復重試。

## Pipeline

```text
Context Retriever
  -> SQL Generator
  -> AST Validator (安全與白名單)
  -> DB Validator (EXPLAIN)
      -> pass -> Executor
      -> fail -> SQL Generator (with db_error)
  -> Final Summarizer

Executor
  -> success -> Final Summarizer
  -> fail    -> SQL Generator (with db_error)
```

## Node 責任

1. Context Retriever
- 載入 schema DDL、enum、business rules、few-shot。

2. SQL Generator
- 生成 1 條候選 SQL。
- 若 State 有 `db_error`，根據錯誤訊息重寫 SQL。

3. AST Validator
- 僅做確定性安全檢查：
  - 只允許 SELECT/WITH 查詢。
  - 只允許 schema 中存在的表與欄位（含 alias 解析，fail-open）。
- 對最外層查詢注入或收斂 `LIMIT 500`。

4. DB Validator
- 對 AST 通過的 SQL 執行 `EXPLAIN`。
- 若 EXPLAIN 失敗，保留 MySQL 原生錯誤至 `db_error` 供重試。

5. Executor
- 實際執行 SQL（唯讀 + timeout）。
- 成功結果交給 summarizer。
- 若執行錯誤，將代表性錯誤寫入 `db_error` 並觸發重試。

6. Final Summarizer
- 將已成功執行的結果轉成最終文字回答。

## Retry Policy

- `MAX_RETRIES = 2`
- 路由條件採 `retry_count < MAX_RETRIES`
- 達到上限即進入 `error_end`

## Result Size Policy

- 預設由 AST 層保證最外層 `LIMIT 500`。
- Executor 保留防禦性檢查，若結果仍超過 `MAX_RESULT_ROWS=500`，視為錯誤回修復迴圈。

## State（精簡）

主要欄位如下：
- `user_query`
- `schema_ddl`, `enum_text`, `rules_text`, `few_shot_examples`
- `candidate_sqls`, `valid_sqls`
- `sql_validated`, `db_error`, `retry_count`
- `champion_sql`, `champion_result`
- `final_answer`, `error_message`

## 執行

在專案根目錄執行：

```bash
python -m langgraph_sql.main
```
