"""Direct HTTP client for Doubao API — replaces Playwright browser automation.

Uses httpx with Chrome header spoofing and sessionid cookies. No browser,
no Argus SDK, no a_bogus signing needed.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, AsyncGenerator, Dict, Optional

import httpx

logger = logging.getLogger("llm_service.http_client")

BASE_URL = "https://www.doubao.com"

# Spoof Chrome 131 on Windows
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Referer": "https://www.doubao.com/chat/",
    "Origin": "https://www.doubao.com",
}


class DoubaoHTTPClient:
    """Direct HTTP client for Doubao's internal API."""

    def __init__(self, device_id: str, web_id: str, timeout: int = 180):
        self.device_id = device_id
        self.web_id = web_id
        self.timeout = timeout
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers=BROWSER_HEADERS,
            timeout=httpx.Timeout(timeout, connect=10),
            follow_redirects=True,
        )

    def _build_cookies(self, session_id: str) -> dict:
        return {
            "sessionid": session_id,
            "sessionid_ss": session_id,
        }

    def _build_params(self) -> dict:
        return {
            "aid": "497858",
            "device_id": self.device_id,
            "device_platform": "web",
            "language": "zh",
            "pc_version": "3.17.0",
            "real_aid": "497858",
            "region": "",
            "samantha_web": "1",
            "sys_region": "",
            "use-olympus-account": "1",
            "version_code": "20800",
            "web_id": self.web_id,
            "web_tab_id": str(uuid.uuid4()),
        }

    async def chat_completion_stream(
        self, payload: dict, session_id: str
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Real SSE streaming — yields parsed events as they arrive.

        Event types:
          {type: "text", content: str}           — text chunk
          {type: "done", conversation_id: str}   — stream complete
          {type: "error", error: str}            — error occurred
        """
        params = self._build_params()
        cookies = self._build_cookies(session_id)
        conversation_id: Optional[str] = None

        try:
            async with self._client.stream(
                "POST",
                "/samantha/chat/completion",
                params=params,
                cookies=cookies,
                json=payload,
                headers={"Content-Type": "application/json"},
            ) as response:
                if response.status_code != 200:
                    body = await response.aread()
                    error_text = body.decode("utf-8", errors="replace")[:500]
                    yield {"type": "error", "error": f"HTTP {response.status_code}: {error_text}"}
                    return

                current_event: Optional[str] = None
                buffer = ""

                async for line in response.aiter_lines():
                    line = line.strip()

                    if not line:
                        current_event = None
                        continue

                    if line.startswith("event:"):
                        current_event = line[6:].strip()
                        continue

                    if not line.startswith("data:"):
                        continue

                    data_str = line[5:].strip()
                    if not data_str or data_str == "{}":
                        continue

                    try:
                        data = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    event = self._parse_sse_event(data, current_event)
                    if event is None:
                        continue

                    if event.get("conversation_id"):
                        conversation_id = event["conversation_id"]

                    if event["type"] == "text":
                        yield event
                    elif event["type"] == "error":
                        yield event
                        return
                    elif event["type"] == "done":
                        event["conversation_id"] = conversation_id
                        yield event
                        return

                # Stream ended without explicit done event
                yield {"type": "done", "conversation_id": conversation_id}

        except httpx.TimeoutException:
            yield {"type": "error", "error": "Request timed out"}
        except Exception as e:
            yield {"type": "error", "error": str(e)}

    def _parse_sse_event(
        self, data: dict, event_name: Optional[str]
    ) -> Optional[Dict[str, Any]]:
        """Parse a single SSE data payload into a normalized event."""
        # Named event format (SSE_REPLY, SSE_CHUNK, etc.)
        if event_name == "STREAM_ERROR":
            code = data.get("error_code", 0)
            msg = data.get("error_msg", "Unknown error")
            return {"type": "error", "error": f"{code}: {msg}"}

        if event_name == "SSE_CONVERSATION_CREATED":
            return {"type": "meta", "conversation_id": data.get("conversation_id")}

        if event_name in ("SSE_REPLY", "SSE_CHUNK"):
            return self._parse_content_event(data)

        if event_name == "SSE_REPLY_END":
            return {"type": "done", "conversation_id": data.get("conversation_id")}

        # Old format: event_type inside data
        event_type = data.get("event_type")
        if event_type is not None:
            return self._parse_legacy_event(data, event_type)

        return None

    def _parse_content_event(self, data: dict) -> Optional[Dict[str, Any]]:
        """Parse content from SSE_REPLY/SSE_CHUNK format."""
        msg = data.get("message", data)
        conversation_id = data.get("conversation_id") or msg.get("conversation_id")
        text = ""

        # Check is_finish flag
        is_finish = msg.get("is_finish", False)

        # Try content_block (full replacement text)
        if msg.get("content_block"):
            for block in msg["content_block"]:
                tb = (block.get("content") or {}).get("text_block")
                if tb and tb.get("text"):
                    text = tb["text"]

        # Try chunk_delta (incremental)
        if data.get("chunk_delta"):
            text = data["chunk_delta"]

        if is_finish:
            return {"type": "done", "conversation_id": conversation_id}

        if text:
            return {"type": "text", "content": text, "conversation_id": conversation_id}

        return None

    def _parse_legacy_event(self, data: dict, event_type: int) -> Optional[Dict[str, Any]]:
        """Parse old format with event_type codes inside data."""
        event_data_raw = data.get("event_data", "{}")
        if isinstance(event_data_raw, str):
            try:
                event_data = json.loads(event_data_raw)
            except json.JSONDecodeError:
                event_data = {}
        else:
            event_data = event_data_raw

        # 2005 = error
        if event_type == 2005:
            code = event_data.get("code", 0)
            msg = event_data.get("msg", "Unknown error")
            return {"type": "error", "error": f"{code}: {msg}"}

        # 2002 = conversation created
        if event_type == 2002:
            return {"type": "meta", "conversation_id": event_data.get("conversation_id")}

        # 2003 = done
        if event_type == 2003:
            return {"type": "done", "conversation_id": event_data.get("conversation_id")}

        # 2001 = text content
        if event_type == 2001:
            text = ""
            msg = event_data.get("message", {})
            if msg.get("content_block"):
                for block in msg["content_block"]:
                    tb = (block.get("content") or {}).get("text_block")
                    if tb and tb.get("text"):
                        text = tb["text"]
            elif msg.get("content"):
                try:
                    cj = json.loads(msg["content"])
                    text = cj.get("text", "")
                except (json.JSONDecodeError, TypeError):
                    pass
            if text:
                return {
                    "type": "text",
                    "content": text,
                    "conversation_id": event_data.get("conversation_id"),
                }

        return None

    async def chat_completion(self, payload: dict, session_id: str) -> dict:
        """Non-streaming: collect full response and return.

        Returns {error, content, conversation_id}.
        """
        content = ""
        conversation_id = None
        error = None

        async for event in self.chat_completion_stream(payload, session_id):
            if event["type"] == "text":
                content = event.get("content", "")
            elif event["type"] == "done":
                conversation_id = event.get("conversation_id")
            elif event["type"] == "error":
                error = event.get("error")
                break
            elif event["type"] == "meta":
                conversation_id = event.get("conversation_id") or conversation_id

        if error and not content:
            return {"error": error, "content": "", "conversation_id": None}

        return {"error": None, "content": content, "conversation_id": conversation_id}

    async def delete_thread(self, conversation_id: str, session_id: str) -> bool:
        """Delete a conversation thread to keep the account clean."""
        if not conversation_id:
            return False
        try:
            cookies = self._build_cookies(session_id)
            resp = await self._client.post(
                "/samantha/thread/delete",
                cookies=cookies,
                json={"conversation_id": conversation_id},
            )
            ok = resp.status_code == 200
            if ok:
                logger.debug("Deleted thread %s", conversation_id)
            else:
                logger.warning("Failed to delete thread %s: HTTP %d", conversation_id, resp.status_code)
            return ok
        except Exception as e:
            logger.warning("Error deleting thread %s: %s", conversation_id, e)
            return False

    async def check_token(self, session_id: str) -> bool:
        """Validate a sessionid by checking if account info returns a user_id."""
        try:
            cookies = self._build_cookies(session_id)
            resp = await self._client.post(
                "/passport/account/info/v2",
                cookies=cookies,
                params={"account_sdk_source": "web"},
            )
            if resp.status_code != 200:
                return False
            data = resp.json()
            return bool(data.get("user_id") or data.get("data", {}).get("user_id"))
        except Exception as e:
            logger.warning("Token check failed: %s", e)
            return False

    async def close(self) -> None:
        await self._client.aclose()
