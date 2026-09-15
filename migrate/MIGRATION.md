# 換機清單

三樣東西要過去，只有第一樣走 Git：

| 內容 | 走 Git？ | 為什麼 |
|---|---|---|
| 程式碼、題庫、GT、驗收集、ARCHITECTURE | ✅ | 全在版控裡 |
| 資料庫（93 表 / 10,213 列） | ❌ 手動 | 倉庫是公開的，資料不上傳 |
| `.env`、向量快取、`eval/results/` | ❌ 手動 | 金鑰不進版控；快取重建要打 API |

---

## 一、舊電腦：打包（已經做好了）

`migrate/` 底下已經有 `ecommerce_demo.sql`、`requirements.lock.txt`、`.env.example`。
還要補進這一包的：

```powershell
cd C:\Text-to-SQL
Copy-Item .env                      migrate\
Copy-Item .table_vectors.json       migrate\
Copy-Item .column_vectors.json      migrate\
Copy-Item .column_hint_vectors.json migrate\
Copy-Item -Recurse eval\results     migrate\eval_results
Compress-Archive migrate\* C:\Users\$env:USERNAME\Desktop\t2s_bundle.zip -Force
```

把 `t2s_bundle.zip`（約 70 MB）用隨身碟或雲端搬到新電腦。**不要用郵件或聊天軟體 —— 裡面有 API 金鑰。**

---

## 二、新電腦：先裝這些

- **Git**
- **Python 3.12.10**（次版本要對上；`.venv` 不要複製，那是絕對路徑綁死的）
- **Docker Desktop**

---

## 三、新電腦：還原

```powershell
# 1. 抓程式碼
git clone https://github.com/Allen900913/Text-to-SQL.git C:\Text-to-SQL
cd C:\Text-to-SQL
# main 與 wide-tables-86 已於 2026-09-15 合併，兩者同為 172c3a7，clone 完就是最新的

# 2. 解開 bundle
Expand-Archive <你的路徑>\t2s_bundle.zip -DestinationPath .\migrate -Force
Copy-Item migrate\.env                      .\
Copy-Item migrate\.table_vectors.json       .\
Copy-Item migrate\.column_vectors.json      .\
Copy-Item migrate\.column_hint_vectors.json .\
New-Item -ItemType Directory -Force eval\results
Copy-Item migrate\eval_results\* eval\results\

# 3. 套件（用 lock 檔，不要用 requirements.txt —— 那份沒鎖版本）
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r migrate\requirements.lock.txt

# 4. 起資料庫
docker compose -f db\docker-compose.yml up -d
# 等 healthy（約 30 秒）
docker ps

# 5. 灌資料 —— 是還原 dump，不是跑 init_db.py（理由見最後一節）
Get-Content migrate\ecommerce_demo.sql -Raw -Encoding UTF8 | docker exec -i text_to_sql_mysql mysql -uroot -p123456 --default-character-set=utf8mb4
```

---

## 四、驗收（跑完這四項才算搬好）

```powershell
$env:PYTHONIOENCODING="utf-8"

# [1] 資料庫形狀 —— 應為 93 / 10213 / 98 / 1071
.\.venv\Scripts\python.exe -c "from langgraph_sql.utils.db_manager import get_db_manager; from langgraph_sql.config import MYSQL_URI; from sqlalchemy import text; c=get_db_manager(MYSQL_URI).engine.connect(); p=lambda s:print(c.execute(text(s)).scalar()); p(\"SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='ecommerce_demo'\"); p(\"SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='ecommerce_demo' AND data_type='enum'\"); p(\"SELECT COUNT(*) FROM information_schema.columns WHERE table_schema='ecommerce_demo' AND column_comment<>''\")"

# [2] Schema 產生鏈沒斷（[[schema-source-must-be-wired]]）
.\.venv\Scripts\python.exe tools\check_schema_pipeline.py

# [3] 向量快取有沒有被接受 —— 若這步開始大量打 API，表示 schema 雜湊對不上，快取白搬了
.\.venv\Scripts\python.exe tools\check_table_retrievability.py

# [4] 端到端一題
.\.venv\Scripts\python.exe -c "from langgraph_sql.graph import compiled_graph; s=compiled_graph.invoke({'user_query':'銷量最高的三個商品是什麼','retry_count':0}); print(s.get('champion_sql')); print(s.get('final_answer'))"
```

---

## 五、為什麼不能用 `init_db.py` 重建資料庫

`init_db.py` 的亂數有固定種子（`DATA_RANDOM_SEED=20260815`）、時間也有固定錨點
（`langgraph_sql/data_anchor.py`），單獨跑確實是決定性的。**但現在這個庫不是它一支跑出來的。**

它是 `init_db.py` 之後又疊了二十幾支工具的結果：`add_wide_tables*`、`add_domain_tables*`、
`add_distractor_tables`、`seed_distractor_data`、`fix_derived_consistency`、
`migrate_enums_to_types`（98 欄搬進 ENUM）、`sync_table_comments`、
`strip_enum_codes` / `strip_dead_enum_values` / `restore_domain_values`（其中有一段是先刪後補）……

順序錯一步，值域、註解、衍生欄位就跟 `eval_ground_truth.yaml` 對不上，
而 GT 對不上的評估是不能歸因的（[[gt-before-test]]）。

**所以資料庫的搬法是還原 dump，不是重跑腳本。** 這份 dump 已經在舊機上驗過往返：
93/93 表 checksum 相同、1071 欄的型別與註解逐列相同、索引 196 與外鍵 105 全同。

---

## 六、順手記一下

- `qdrant` 容器雖然在跑，但**全專案沒有一行程式碼引用它** —— 新電腦不用起它。
- `venv/`（舊的）和 `.venv/` 都不要複製，在新機重建。
- `logs/` 不用搬。
- `.column_hint_vectors.json` 有 26 MB，重建等於把 1071 個欄位重新嵌入一次；
  現在 429 還沒完全解除，能搬就搬。
