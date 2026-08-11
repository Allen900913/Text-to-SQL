import os
from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

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

# NVIDIA API Key
NVIDIA_API_KEY = os.getenv("NVIDA_API_KEY")

if not NVIDIA_API_KEY:
    raise ValueError("未在環境變數中找到 NVIDA_API_KEY，請確認 .env 檔案配置正確。")

# 初始化 LLM 模型 (使用 OpenAI Client 串接 NVIDIA API)
llm = ChatOpenAI(
    model="meta/llama-3.1-70b-instruct",
    api_key=NVIDIA_API_KEY,
    base_url="https://integrate.api.nvidia.com/v1",
    temperature=0,
    request_timeout=300.0,
)

# Agent 設定
MAX_RETRIES = 3     # SQL 重試最大次數（由 AgentExecutor max_iterations 控制）
