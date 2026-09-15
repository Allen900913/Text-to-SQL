# 換機清單

**這一份就是全部，照著做就好。** 新電腦上先讀 zip 裡的這一份（repo 還沒 clone 下來）。

三樣東西要過去，只有第一樣走 Git：

| 內容 | 走 Git？ | 為什麼 |
|---|---|---|
| 程式碼、題庫、GT、驗收集、ARCHITECTURE | ✅ clone 就有 | 全在版控裡 |
| 資料庫（93 表 / 10,213 列） | ❌ 在 bundle 裡 | 倉庫是公開的，資料不上傳 |
| `.env`、向量快取、`eval/results/` | ❌ 在 bundle 裡 | 金鑰不進版控；快取重建要打 API |

`t2s_bundle.zip`（22 MB）**用隨身碟或雲端搬，不要走郵件或聊天軟體 —— 裡面有 API 金鑰。**

---

## 一、新電腦：先裝這三個

- **Git**
- **Python 3.12.10** —— 次版本要對上。`.venv` **不要複製**，那是絕對路徑綁死的，複製過去會壞。
- **Docker Desktop**

> Docker Desktop 裝好後要確認它真的在跑（`docker ps` 不報錯）。
> 舊機上它不在預設的 `C:\Program Files\Docker\`，而在 `%LOCALAPPDATA%\Programs\DockerDesktop\`。

---

## 二、新電腦：還原

以下整段在 **PowerShell** 執行。

```powershell
# ── 1. 抓程式碼 ───────────────────────────────────────────────
git clone https://github.com/Allen900913/Text-to-SQL.git C:\Text-to-SQL
cd C:\Text-to-SQL
# main 與 wide-tables-86 已於 2026-09-15 合併成同一個 commit，clone 完就是最新的，不必切分支

# ── 2. 解開 bundle ───────────────────────────────────────────
# 注意：解到 C:\t2s_in，不要解到 .\migrate —— 那個資料夾裡有版控中的檔案，
# 解壓縮覆蓋過去會讓 git status 出現假的修改。
Expand-Archive <你放 zip 的路徑>\t2s_bundle.zip -DestinationPath C:\t2s_in -Force

Copy-Item C:\t2s_in\.env                      .\
Copy-Item C:\t2s_in\.table_vectors.json       .\
Copy-Item C:\t2s_in\.column_vectors.json      .\
Copy-Item C:\t2s_in\.column_hint_vectors.json .\
New-Item -ItemType Directory -Force eval\results | Out-Null
Copy-Item C:\t2s_in\eval_results\* eval\results\

# ── 3. 套件 ──────────────────────────────────────────────────
# 用 lock 檔。requirements.txt 一個版本都沒鎖，照它裝會拿到不同版本。
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r C:\t2s_in\requirements.lock.txt

# ── 4. 起資料庫 ──────────────────────────────────────────────
docker compose -f db\docker-compose.yml up -d
docker ps          # 等 text_to_sql_mysql 顯示 (healthy)，約 30 秒

# ── 5. 灌資料 ────────────────────────────────────────────────
# 一定要用 cmd 的 `<` 重導向。理由見第四節 —— 這一步錯了中文會靜默爛掉。
cmd /c "docker exec -i text_to_sql_mysql mysql -uroot -p123456 --default-character-set=utf8mb4 < C:\t2s_in\ecommerce_demo.sql"
```

---

## 三、驗收（兩行，跑完才算搬好）

```powershell
$env:PYTHONIOENCODING="utf-8"
.\.venv\Scripts\python.exe tools\check_migration.py        # 資料庫形狀 + 快取 + 金鑰（零 API 呼叫）
.\.venv\Scripts\python.exe tools\check_schema_pipeline.py  # schema→prompt 兩條鏈的八項閘門
```

第一支全過會長這樣（數字是舊機 2026-09-15 實測的，必須逐項相同）：

```
[1] 資料庫形狀
  OK  tables               93     OK  commented_cols     1071
  OK  rows              10213     OK  table_comments       93
  OK  enum_cols            98     OK  indexes             196
                                  OK  foreign_keys        105
[2] 向量快取（零 API 呼叫）
  OK  表向量      93/93 命中快取
  OK  欄位提示向量 869/869 命中快取
[3] 必要檔案 …… 全 OK
```

第二支應該是「八項全部通過」。

有任何一項 X 就**先停下來修，不要跑評估** —— GT 的前提沒對上的話，
模型錯與資料錯會混在一起，評估結果不能歸因。

最後可以端到端試一題：

```powershell
.\.venv\Scripts\python.exe -c "from langgraph_sql.graph import compiled_graph; s=compiled_graph.invoke({'user_query':'銷量最高的三個商品是什麼','retry_count':0}); print(s.get('champion_sql')); print(s.get('final_answer'))"
```

---

## 四、兩個會讓你踩到的坑

### 坑一：不要用 PowerShell 的管線灌 SQL

```powershell
# X 不要這樣 —— 中文會爛掉，而且是靜默的
Get-Content ecommerce_demo.sql -Raw | docker exec -i text_to_sql_mysql mysql ...
```

PowerShell 把文字送給原生 exe 時會依 `$OutputEncoding` 重新編碼，而這個值**是機器相依的**
（PS 5.1 預設 ASCII，但裝了「UTF-8 全球語言支援」的機器是 UTF-8）。
舊機實測過：經過 PowerShell 字串處理之後灌，會直接噴
`ERROR 1064 ... near '?嗡辣鈭粹閰?'`。

`cmd /c "... < file"` 是位元組層重導向，不經任何重新編碼。舊機用它灌完比對過：
**93/93 表 checksum 相同、1071 欄的型別與註解逐列相同、索引 196 與外鍵 105 全同。**

同理，任何時候用 `Get-Content` / `Set-Content` 碰這個 `.sql`，都要明寫 `-Encoding utf8`，
否則 PS 5.1 會用系統 ANSI（中文 Windows 上是 Big5）去讀 UTF-8。

### 坑二：不要用 `init_db.py` 重建資料庫

`init_db.py` 本身是決定性的（種子 `DATA_RANDOM_SEED=20260815`、時間錨點在
`langgraph_sql/data_anchor.py`），所以看起來重跑就會得到一樣的資料。**但現在這個庫不是它一支跑出來的。**

它是 `init_db.py` 之後又疊了二十幾支工具的結果：`add_wide_tables*`、`add_domain_tables*`、
`add_distractor_tables`、`seed_distractor_data`、`fix_derived_consistency`、
`migrate_enums_to_types`（98 欄搬進 ENUM）、`sync_table_comments`、
`strip_enum_codes` / `strip_dead_enum_values` / `restore_domain_values`（其中有一段是先刪後補）……

順序錯一步，值域、註解、衍生欄位就跟 `eval_ground_truth.yaml` 對不上。
**資料庫的搬法是還原 dump，不是重跑腳本。**

---

## 五、順手記一下

- `qdrant` 容器在舊機上是開著的，但**全專案沒有一行程式碼引用它** —— 新電腦不用起。
- `venv/`（舊的）和 `.venv/` 都不要複製，在新機重建。`logs/` 不用搬。
- `.column_hint_vectors.json` 有 26 MB。重建等於把 869 個欄位文件重新嵌入一次，
  而 429 還沒完全解除 —— 能搬就搬。驗收第 [2] 項就是在確認它有沒有被接受。
- `requirements.txt` 原本漏了 `requests` 與 `numpy`（兩個都是直接 import），已補。
  但換機還是用 `requirements.lock.txt`，它鎖了全部 58 個套件的版本。

---

## 附錄：舊電腦怎麼打包的（已經做完，換機時不用看）

```powershell
cd C:\Text-to-SQL
docker exec text_to_sql_mysql mysqldump -uroot -p123456 --databases ecommerce_demo `
  --default-character-set=utf8mb4 --routines --triggers --events `
  --single-transaction --hex-blob --skip-extended-insert > migrate\ecommerce_demo.sql
.\.venv\Scripts\python.exe -m pip freeze | Select-String -NotMatch "^pip==" > migrate\requirements.lock.txt
Copy-Item .env, .table_vectors.json, .column_vectors.json, .column_hint_vectors.json migrate\
Copy-Item -Recurse eval\results migrate\eval_results
Compress-Archive migrate\* "$env:USERPROFILE\Desktop\t2s_bundle.zip" -Force
```
