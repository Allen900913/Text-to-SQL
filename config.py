import os
from dotenv import load_dotenv
from langchain_groq import ChatGroq

# 從 text_to_sql 的父目錄（langchain2/）找 .env 檔案
_env_path = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    '.env'
)
load_dotenv(dotenv_path=_env_path)

# MySQL 連線字串設定
# Port 為 3306，主機為 127.0.0.1
MYSQL_URI = os.getenv(
    "MYSQL_URI",
    "mysql+pymysql://root:123456@127.0.0.1:3306/ecommerce_demo"
)

# Groq API Key
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

if not GROQ_API_KEY:
    raise ValueError("未在環境變數中找到 GROQ_API_KEY，請確認 .env 檔案配置正確。")

# 初始化 LLM 模型
# 使用較大的 70B 模型 (llama-3.3-70b-versatile)，不僅指令遵循度更高，也更能避免小模型胡言亂語或重複呼叫的問題
llm = ChatGroq(
    model="llama-3.3-70b-versatile",
    api_key=GROQ_API_KEY,
    temperature=0,
    request_timeout=60.0,
)

# Agent 設定
MAX_RETRIES = 3     # SQL 重試最大次數（由 AgentExecutor max_iterations 控制）
