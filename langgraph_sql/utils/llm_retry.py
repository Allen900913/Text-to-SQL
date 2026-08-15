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

「回 200 但內容空白」也算故障：
  API 偶爾會成功回應但 content 是空字串。它不拋例外，所以曾經被當成
  正常結果往下傳，接著在 _parse_sql 失敗、消耗一格 Pipeline retry，
  而且 llm_error 是空的 —— 評估時會被歸類成 error_end（模型能力不足），
  實際上是基礎設施問題。100 題實測中 5 題 error_end 有 4 題是這個原因。
"""
import time

from loguru import logger as log

from langgraph_sql.config import LLM_MAX_ATTEMPTS, LLM_BACKOFF_BASE


class _BlankResponse(Exception):
    """API 回應成功但內容空白。與網路故障走同一條退避重試路徑。"""


def invoke_with_retry(llm, messages, tag: str = "LLM") -> tuple[str, str]:
    """
    呼叫 LLM，暫時性失敗時以指數退避重試。

    回傳 (content, error)：
      - 成功 → (非空的回覆內容, "")
      - 全部嘗試失敗 → ("", 最後一次的錯誤字串)

    呼叫端只要檢查 error 是否為空即可；content 為空一定伴隨非空的 error。
    """
    last_error = ""

    for attempt in range(1, LLM_MAX_ATTEMPTS + 1):
        try:
            response = llm.invoke(messages)
            content = response.content if hasattr(response, "content") else str(response)
            if not (content or "").strip():
                raise _BlankResponse("API 回應成功但 content 為空")
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
