"""
NVIDIA NIM LLM Benchmark Script
測試所有 LLM 模型的回應速度（TTFT & 總生成時間）
"""

import sys
import os
import time
import json
import asyncio
import aiohttp
from datetime import datetime
from typing import Optional

# Force UTF-8 output on Windows
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# ── 設定 ──────────────────────────────────────────────────────────────────────
API_KEY = "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"
BASE_URL = "https://integrate.api.nvidia.com/v1"
TEST_PROMPT = "What is 1+1? Reply in one word."
MAX_TOKENS = 50
CONCURRENT_LIMIT = 5  # 同時最多 5 個請求，避免 rate limit
TIMEOUT_SECONDS = 60

# ── 要測試的 LLM 模型清單 ─────────────────────────────────────────────────────
LLM_MODELS = [
    "01-ai/yi-large",
    "ai21labs/jamba-1.5-large-instruct",
    "databricks/dbrx-instruct",
    "deepseek-ai/deepseek-coder-6.7b-instruct",
    "deepseek-ai/deepseek-v4-flash-0731",
    "deepseek-ai/deepseek-v4-pro-0813",
    "google/gemma-3-12b-it",
    "google/gemma-3-4b-it",
    "google/gemma-4-31b-it",
    "ibm/granite-3.0-3b-a800m-instruct",
    "ibm/granite-3.0-8b-instruct",
    "ibm/granite-34b-code-instruct",
    "ibm/granite-8b-code-instruct",
    "meta/codellama-70b",
    "meta/llama2-70b",
    "meta/muse-glimmer-30b",
    "microsoft/phi-3.5-moe-instruct",
    "minimaxai/minimax-m3",
    "mistralai/codestral-22b-instruct-v0.1",
    "mistralai/mistral-7b-instruct-v0.3",
    "mistralai/mistral-large",
    "mistralai/mistral-large-2-instruct",
    "mistralai/mistral-nemotron",
    "mistralai/mixtral-8x22b-v0.1",
    "moonshotai/kimi-k2.6",
    "moonshotai/kimi-k3",
    "nv-mistralai/mistral-nemo-12b-instruct",
    "nvidia/llama-3.1-nemotron-51b-instruct",
    "nvidia/llama-3.1-nemotron-70b-instruct",
    "nvidia/llama-3.1-nemotron-ultra-253b-v1",
    "nvidia/llama3-chatqa-1.5-70b",
    "nvidia/mistral-nemo-minitron-8b-8k-instruct",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-4-340b-instruct",
    "nvidia/nemotron-nano-3-30b-a3b",
    "openai/gpt-oss-20b",
    "poolside/laguna-xs-2.1",
    "writer/palmyra-creative-122b",
    "writer/palmyra-fin-70b-32k",
    "writer/palmyra-med-70b",
    "writer/palmyra-med-70b-32k",
    "zyphra/zamba2-7b-instruct",
]


async def test_model_streaming(
    session: aiohttp.ClientSession,
    model_id: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """用 streaming 方式測試單個模型，記錄 TTFT 與總時間"""
    async with semaphore:
        result = {
            "model": model_id,
            "ttft_ms": None,
            "total_ms": None,
            "output_text": None,
            "status": "pending",
            "error": None,
        }

        headers = {
            "Authorization": f"Bearer {API_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }

        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": TEST_PROMPT}],
            "max_tokens": MAX_TOKENS,
            "stream": True,
        }

        t_start = time.perf_counter()
        t_first_token = None
        collected_text = []

        try:
            timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
            async with session.post(
                f"{BASE_URL}/chat/completions",
                headers=headers,
                json=payload,
                timeout=timeout,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    result["status"] = "error"
                    result["error"] = f"HTTP {resp.status}: {body[:200]}"
                    return result

                async for raw_line in resp.content:
                    line = raw_line.decode("utf-8").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    choices = chunk.get("choices", [])
                    if not choices:
                        continue

                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")

                    if content:
                        if t_first_token is None:
                            t_first_token = time.perf_counter()
                        collected_text.append(content)

            t_end = time.perf_counter()

            result["ttft_ms"] = round((t_first_token - t_start) * 1000, 1) if t_first_token else None
            result["total_ms"] = round((t_end - t_start) * 1000, 1)
            result["output_text"] = "".join(collected_text).strip()
            result["status"] = "ok"

        except asyncio.TimeoutError:
            result["status"] = "timeout"
            result["error"] = f"Timeout after {TIMEOUT_SECONDS}s"
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)[:200]

        return result


async def main():
    print("=" * 90)
    print(f"[START] NVIDIA NIM LLM Benchmark")
    print(f"   測試提示: \"{TEST_PROMPT}\"")
    print(f"   模型數量: {len(LLM_MODELS)}")
    print(f"   並行限制: {CONCURRENT_LIMIT}")
    print(f"   Timeout : {TIMEOUT_SECONDS}s")
    print(f"   開始時間: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 90)

    semaphore = asyncio.Semaphore(CONCURRENT_LIMIT)
    results = []
    completed = 0

    connector = aiohttp.TCPConnector(limit=20)
    async with aiohttp.ClientSession(connector=connector) as session:
        tasks = [
            test_model_streaming(session, model_id, semaphore)
            for model_id in LLM_MODELS
        ]

        for coro in asyncio.as_completed(tasks):
            r = await coro
            completed += 1
            results.append(r)

            status_icon = {"ok": "[OK]", "error": "[ERR]", "timeout": "[TIMEOUT]"}.get(r["status"], "[?]")
            model_short = r["model"].split("/")[-1][:35]
            if r["status"] == "ok":
                ttft = f"{r['ttft_ms']:.0f} ms" if r["ttft_ms"] else "N/A"
                total = f"{r['total_ms']:.0f} ms"
                text = (r["output_text"] or "")[:40].replace("\n", " ")
                print(f"{status_icon} [{completed:>2}/{len(LLM_MODELS)}] {model_short:<36} TTFT={ttft:>8}  Total={total:>8}  \"{text}\"")
            else:
                err = (r["error"] or "")[:60]
                print(f"{status_icon} [{completed:>2}/{len(LLM_MODELS)}] {model_short:<36} {r['status'].upper()}: {err}")

    # ── 排序並輸出最終報告 ────────────────────────────────────────────────────
    ok_results = sorted(
        [r for r in results if r["status"] == "ok" and r["ttft_ms"] is not None],
        key=lambda x: x["ttft_ms"]
    )
    fail_results = [r for r in results if r["status"] != "ok"]

    print("\n")
    print("=" * 90)
    print("[RESULT] 最終排名（依 TTFT 由快至慢）")
    print("=" * 90)
    print(f"{'#':<4} {'模型 ID':<45} {'TTFT':>10} {'總時間':>10}  回覆")
    print("-" * 90)
    for i, r in enumerate(ok_results, 1):
        ttft = f"{r['ttft_ms']:.0f} ms"
        total = f"{r['total_ms']:.0f} ms"
        text = (r["output_text"] or "")[:30].replace("\n", " ")
        print(f"{i:<4} {r['model']:<45} {ttft:>10} {total:>10}  \"{text}\"")

    if fail_results:
        print("\n[FAILED] 失敗/超時模型:")
        for r in fail_results:
            print(f"   {r['model']:<45} [{r['status'].upper()}] {r['error'] or ''}")

    # ── 儲存結果 ──────────────────────────────────────────────────────────────
    ts = int(time.time())
    script_dir = os.path.dirname(os.path.abspath(__file__))
    out_dir = os.path.join(script_dir, "results")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"nvidia_llm_benchmark_{ts}.json")

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "timestamp": datetime.now().isoformat(),
                "prompt": TEST_PROMPT,
                "results": sorted(results, key=lambda x: x["ttft_ms"] or 99999),
            },
            f,
            ensure_ascii=False,
            indent=2,
        )
    print(f"\n[SAVED] 結果已儲存至: {out_path}")
    print("=" * 90)


if __name__ == "__main__":
    asyncio.run(main())
