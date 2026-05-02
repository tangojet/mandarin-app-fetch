"""Doubao (豆包) extractor — uses the integrated LLMProvider directly.

Calls the local LLMProvider (no HTTP round-trip) to summarize URLs.
Falls back gracefully if the provider isn't configured or initialised.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("extractor.doubao")


def is_configured() -> bool:
    """Return True if the LLM provider is initialised and available."""
    from main import get_llm_provider
    return get_llm_provider() is not None


async def extract_with_doubao(url: str) -> str | None:
    """Summarize a URL using the integrated LLM provider.

    Returns the summary text, or None on failure.
    """
    from main import get_llm_provider

    provider = get_llm_provider()
    if not provider:
        logger.warning("LLM provider not available, skipping")
        return None

    model = provider.config.default_model
    prompt = f"请总结这个链接的内容：{url}"

    logger.info("summarizing %s via LLMProvider (model=%s)", url, model)

    try:
        request_data = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
        }
        response = await provider.chat_completion(request_data)

        # response is a JSONResponse — extract the body
        data = response.body
        if isinstance(data, bytes):
            import json
            data = json.loads(data)

        if data.get("error"):
            logger.warning("Provider returned error: %s", data["error"].get("message"))
            return None

        content = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        if not content:
            logger.warning("Empty response from provider")
            return None

        logger.info("Got summary (%d chars)", len(content))
        return content

    except Exception as e:
        logger.warning("Error: %s", e)
        return None
