"""Doubao (豆包) extractor — OpenAI-compatible chat completions API.

Calls a doubao-2api compatible endpoint to summarize URLs via Doubao's LLM.

Env vars:
  DOUBAO_API_URL   — Required, e.g. http://localhost:8088
  DOUBAO_API_KEY   — Optional API key (maps to API_MASTER_KEY in doubao-2api)
  DOUBAO_MODEL     — Model name (default: doubao-1.5-pro)
  DOUBAO_TIMEOUT   — Timeout in seconds (default: 120)
"""

from __future__ import annotations

import logging
import os

import httpx

logger = logging.getLogger("extractor.doubao")

DEFAULT_MODEL = "doubao-1.5-pro"
DEFAULT_TIMEOUT = 120


def _get_config() -> tuple[str | None, str | None, str, int]:
    """Return (api_url, api_key, model, timeout)."""
    api_url = os.environ.get("DOUBAO_API_URL")
    api_key = os.environ.get("DOUBAO_API_KEY")
    model = os.environ.get("DOUBAO_MODEL", DEFAULT_MODEL)
    timeout = int(os.environ.get("DOUBAO_TIMEOUT", str(DEFAULT_TIMEOUT)))
    return api_url, api_key, model, timeout


def is_configured() -> bool:
    """Return True if DOUBAO_API_URL is set."""
    return bool(os.environ.get("DOUBAO_API_URL"))


async def extract_with_doubao(url: str) -> str | None:
    """Summarize a URL using Doubao via doubao-2api.

    Returns the summary text, or None on failure.
    """
    api_url, api_key, model, timeout = _get_config()
    if not api_url:
        logger.warning("DOUBAO_API_URL not set, skipping")
        return None

    endpoint = f"{api_url.rstrip('/')}/v1/chat/completions"
    prompt = f"请总结这个链接的内容：{url}"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    logger.info("summarizing %s via %s (model=%s)", url, endpoint, model)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                endpoint,
                headers=headers,
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "stream": False,
                },
            )

        if resp.status_code != 200:
            logger.warning("API error %d: %s", resp.status_code, resp.text[:500])
            return None

        data = resp.json()
        if data.get("error"):
            logger.warning("API returned error: %s", data["error"].get("message"))
            return None

        content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        if not content:
            logger.warning("empty response from API")
            return None

        logger.info("got summary (%d chars)", len(content))
        return content

    except httpx.TimeoutException:
        logger.warning("request timed out after %ds", timeout)
        return None
    except Exception as e:
        logger.warning("error: %s", e)
        return None
