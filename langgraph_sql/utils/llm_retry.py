"""
LLM 呼叫的退避重試
====================
NVIDIA API 在連續壓測時會出現 `Request timed out` 與
`503 ResourceExhausted: Worker local total request limit reached`。
這類故障是暫時性的，重試通常就會成功。

為什麼要在這裡重試，而不是靠 Pipeline 的 retry_count：
  Pipeline 的 MAX_RETRIES 是給「模型寫錯 SQL」用的預算（目前只有 2）。
  若讓一次網路逾時吃掉一格，等於用修 bug 的預算去扛基礎設施故障，
  而且評估報告會把 API 逾時記成 error_end，與模型能力不足混為一談。
"""
import time

from loguru import logger as log

from langgraph_sql.config import LLM_MAX_ATTEMPTS, LLM_BACKOFF_BASE


def invoke_with_retry(llm, messages, tag: str = "LLM") -> tuple[str, str]:
    """
    呼叫 LLM，暫時性失敗時以指數退避重試。

    回傳 (content, error)：
      - 成功 → (回覆內容, "")
      - 全部嘗試失敗 → ("", 最後一次的錯誤字串)
    """
    last_error = ""

    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            response = llm.invoke(messages)
            content = response.content if hasattr(response, "content") else str(response)
            if attempt > 1:
                log.info(f"{tag} 第 {attempt} 次嘗試成功")
            return content, ""
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if attempt >= LLM_MAX_ATTEMPTS:
                log.error(f"{tag} 已重試 {LLM_MAX_ATTEMPTS} 次仍失敗: {last_error}")
                break
            wait = LLM_BACKOFF_BASE * (2 ** (attempt - 1))
            log.warning(
                f"{tag} 第 {attempt}/{LLM_MAX_ATTEMPTS} 次呼叫失敗（{last_error}），"
                f"{wait}s 後重試"
            )
            time.sleep(wait)

    return "", last_error
