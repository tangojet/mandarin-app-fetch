"""Tencent Yuanbao (元宝) extractor — SSE streaming client.

WeChat article URLs (mp.weixin.qq.com) are blocked for external crawlers.
Tencent Yuanbao has privileged access to WeChat content and can summarize articles.

Env vars:
  YUANBAO_COOKIE_FILE   — Path to cookie file (default: ~/.mandarin-app-fetch/yuanbao-cookies.txt)
  YUANBAO_AGENT_ID      — Agent ID (default: naQivTmsDa)
  YUANBAO_CHAT_MODEL_ID — Chat model ID (default: deep_seek_v3)
"""

from __future__ import annotations

import json
import logging
import os

import httpx

import cookie_manager

logger = logging.getLogger("extractor.yuanbao")

YUANBAO_BASE = "https://yuanbao.tencent.com"
DEFAULT_AGENT_ID = "naQivTmsDa"
DEFAULT_CHAT_MODEL_ID = "deep_seek_v3"


def _get_config() -> tuple[str, str]:
    """Return (agent_id, chat_model_id)."""
    agent_id = os.environ.get("YUANBAO_AGENT_ID", DEFAULT_AGENT_ID)
    chat_model_id = os.environ.get("YUANBAO_CHAT_MODEL_ID", DEFAULT_CHAT_MODEL_ID)
    return agent_id, chat_model_id


def is_configured() -> bool:
    """Return True if yuanbao cookies are available."""
    return cookie_manager.load_cookies("yuanbao") is not None


def _load_cookies() -> str | None:
    """Read cookies as a raw header string."""
    return cookie_manager.load_cookies_as_header("yuanbao")


async def _create_conversation(agent_id: str, cookies: str) -> str | None:
    """Create a new Yuanbao conversation, return conversation ID or None."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{YUANBAO_BASE}/api/user/agent/conversation/create",
            headers={"Content-Type": "application/json", "Cookie": cookies},
            json={"agentId": agent_id},
        )
    if resp.status_code != 200:
        logger.warning("conversation create failed: %d %s", resp.status_code, resp.reason_phrase)
        return None
    data = resp.json()
    if data.get("requireLogin"):
        logger.warning("cookies expired (requireLogin=true)")
        return None
    return data.get("id") or data.get("conversationId")


async def _chat_stream(
    conversation_id: str, url: str, chat_model_id: str, cookies: str
) -> str | None:
    """Send chat request and collect SSE text chunks, return concatenated result."""
    prompt = f"请总结这篇微信文章的内容：{url}"
    body_str = json.dumps({
        "model": "gpt_175B_0404",
        "prompt": prompt,
        "plugin": "Adaptive",
        "displayPrompt": prompt,
        "displayPromptType": 1,
        "agentId": DEFAULT_AGENT_ID,
        "chatModelId": chat_model_id,
        "supportFunctions": ["openAutoSearchSwitch", "autoInternetSearch"],
        "multimedia": [],
        "supportHint": 1,
        "version": "v2",
    })

    async with httpx.AsyncClient(timeout=180) as client:
        async with client.stream(
            "POST",
            f"{YUANBAO_BASE}/api/chat/{conversation_id}",
            headers={
                "Content-Type": "text/plain;charset=UTF-8",
                "X-Requested-With": "XMLHttpRequest",
                "Cookie": cookies,
                "Accept": "text/event-stream",
            },
            content=body_str.encode("utf-8"),
        ) as resp:
            if resp.status_code != 200:
                logger.warning("chat request failed: %d", resp.status_code)
                return None

            chunks: list[str] = []
            buffer = ""
            async for raw_bytes in resp.aiter_bytes():
                buffer += raw_bytes.decode("utf-8", errors="replace")
                lines = buffer.split("\n")
                buffer = lines.pop()
                for line in lines:
                    if not line.startswith("data:"):
                        continue
                    json_str = line[5:].strip()
                    if not json_str or json_str == "[DONE]":
                        continue
                    try:
                        evt = json.loads(json_str)
                        if evt.get("requireLogin"):
                            logger.warning("cookies expired during streaming")
                            return None
                        if evt.get("type") == "text" and isinstance(evt.get("msg"), str):
                            chunks.append(evt["msg"])
                    except (json.JSONDecodeError, KeyError):
                        pass

    result = "".join(chunks)
    return result or None


async def extract_with_yuanbao(url: str) -> str | None:
    """Summarize a WeChat article URL using Yuanbao.

    Returns the summary text, or None on failure.
    """
    cookies = _load_cookies()
    if not cookies:
        logger.warning("no cookies available, skipping")
        return None

    agent_id, chat_model_id = _get_config()

    # Step 1: Create conversation
    conversation_id = await _create_conversation(agent_id, cookies)
    if not conversation_id:
        logger.warning("failed to create conversation")
        return None

    logger.info("created conversation %s, fetching summary for %s", conversation_id, url)

    # Step 2: Chat with SSE streaming
    summary = await _chat_stream(conversation_id, url, chat_model_id, cookies)
    if not summary:
        logger.warning("no summary content returned")
        return None

    logger.info("got summary (%d chars)", len(summary))
    return summary
