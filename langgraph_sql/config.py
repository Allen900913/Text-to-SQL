"""
LangGraph Funnel Pipeline — 設定檔
===================================
初始化 LLM 模型、資料庫連線字串、Logger 與全域常數。
"""
import os
import sys
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from loguru import logger as log

# ===========================================================================
# Windows console UTF-8 修正 (已移除，避免背景執行時 crash)
# ===========================================================================
# for _stream in (sys.stdout, sys.stderr):
#     if _stream is not None and getattr(_stream, "encoding", None) != "utf-8":
#         try:
#             _stream.reconfigure(encoding="utf-8", errors="backslashreplace")
#         except (AttributeError, ValueError):
#             pass

# ===========================================================================
# Logger 設定
# ===========================================================================
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_log_dir = os.path.join(_project_root, "logs")
os.makedirs(_log_dir, exist_ok=True)

log.remove()  # 清除預設 handler
log.add(
    sys.stdout,
    level="INFO",
    format=(
        "{time:YYYY-MM-DD HH:mm:ss} | "
        "{module}.{function}:{line} | "
        "{level}: {message}"
    ),
)
log.add(
    os.path.join(_log_dir, "langgraph_pipeline.log"),
    level="DEBUG",
    encoding="UTF-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {module}.{function}:{line} | {message}",
    rotation="10 MB",
    retention="7 days",
)

# ===========================================================================
# .env 載入（從專案根目錄 d:\text_to_sql\.env）
# ===========================================================================
_env_path = os.path.join(_project_root, ".env")
load_dotenv(dotenv_path=_env_path)

# ===========================================================================
# MySQL 連線字串
# ===========================================================================
MYSQL_URI = os.getenv(
    "MYSQL_URI",
    "mysql+pymysql://root:123456@127.0.0.1:3306/ecommerce_demo",
)

# ===========================================================================
# NVIDIA API Key
# ===========================================================================
NVIDIA_API_KEY = os.getenv("NVIDIA_API_KEY")
if not NVIDIA_API_KEY:
    raise ValueError("未在環境變數中找到 NVIDIA_API_KEY，請確認 .env 檔案配置正確。")

# ===========================================================================
# LLM 模型初始化
# ===========================================================================
# 2026-08-16 起 Llama 3.3 70B 停止服務，原本的 meta/llama-3.3-70b-instruct
# (Generator) 與 meta/llama-3.1-70b-instruct (Summarizer) 全數退場。
# 官方建議的替代是 GPT OSS 120B 或 Qwen3.6 27B；本專案掛的 NVIDIA NIM endpoint
# 沒有提供任何 Qwen 模型（已對 /v1/models 清單確認），因此改用 GPT OSS。
# 實測 gpt-oss 雖是 reasoning 模型，但在此 Prompt 下不會外洩思考過程，
# 輸出即為純 SQL，_parse_sql 無需調整；延遲約 2 秒，遠優於原本的 Llama。

# Generator 模型 (120B) — 單次生成 1 條高品質 SQL
llm_fast = ChatOpenAI(
    model="openai/gpt-oss-120b",
    api_key=NVIDIA_API_KEY,
    base_url="https://integrate.api.nvidia.com/v1",
    temperature=0,
    request_timeout=300.0,
)

# 摘要模型 (20B) — 純格式轉換任務，好鋼用在刀刃上。
# 已驗證能正確引用權威筆數而不自行清點 JSON。
llm_summarizer = ChatOpenAI(
    model="openai/gpt-oss-20b",
    api_key=NVIDIA_API_KEY,
    base_url="https://integrate.api.nvidia.com/v1",
    temperature=0,
    request_timeout=300.0,
)

# ===========================================================================
# 全域常數
# ===========================================================================
MAX_RETRIES = 2           # 最大重試次數（重回 Node 2 重新生成的總預算）
SQL_TIMEOUT_MS = 5000     # SQL 執行超時（毫秒）
MAX_RESULT_ROWS = 500     # 結果集上限，超過視為異常丟棄

# LLM 呼叫的退避重試（處理 API 逾時 / 429 / 503 等暫時性故障）。
# 與 MAX_RETRIES 分開計算：基礎設施故障不該消耗修正 SQL 的預算。
LLM_MAX_ATTEMPTS = 3      # 單次節點呼叫的最大嘗試次數
LLM_BACKOFF_BASE = 5      # 退避基數（秒），第 n 次等待 BASE * 2^(n-1)
