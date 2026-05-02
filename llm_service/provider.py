"""LLM provider — OpenAI-compatible chat completions via direct HTTP.

Stateless per-request: each request creates a new conversation, gets the
response, then deletes the thread. No SessionManager needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from llm_service.config import LLMConfig
from llm_service.http_client import DoubaoHTTPClient
from llm_service.sse_utils import DONE_CHUNK, create_chat_completion_chunk, create_sse_data

logger = logging.getLogger("llm_service.provider")


class LLMProvider:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.http_client = DoubaoHTTPClient(
            device_id=config.device_id,
            web_id=config.web_id,
            timeout=config.request_timeout,
        )

    async def initialize(self) -> None:
        """Validate that at least one session token is alive."""
        alive = 0
        for sid in self.config.session_ids:
            masked = sid[:6] + "..." + sid[-4:] if len(sid) > 12 else sid[:4] + "..."
            ok = await self.http_client.check_token(sid)
            if ok:
                alive += 1
                logger.info("Token %s is valid", masked)
            else:
                logger.warning("Token %s is invalid or expired", masked)

        if alive == 0:
            logger.warning("No valid session tokens found — requests will likely fail")
        else:
            logger.info("%d/%d session token(s) valid", alive, len(self.config.session_ids))

    async def close(self) -> None:
        await self.http_client.close()

    def _pick_session_id(self) -> str:
        """Pick a random session ID for load distribution."""
        return random.choice(self.config.session_ids)

    def _build_payload(self, messages: List[Dict[str, Any]], bot_id: str) -> Dict[str, Any]:
        """Build the Doubao v2 chat payload (always new conversation)."""
        last_user_msg = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if not last_user_msg:
            raise HTTPException(status_code=400, detail="No user message found.")

        text = last_user_msg["content"]
        now_ms = int(time.time() * 1000)
        now_sec = int(time.time())

        return {
            "messages": [
                {
                    "content": json.dumps({"text": text}, ensure_ascii=False),
                    "content_type": 2001,
                    "attachments": [],
                    "references": [],
                }
            ],
            "completion_option": {
                "is_regen": False,
                "with_suggest": False,
                "need_create_conversation": True,
                "launch_stage": 1,
                "use_deep_think": False,
                "event_id": "0",
            },
            "conversation_id": "0",
            "local_conversation_id": f"local_{now_ms}_{uuid.uuid4().hex[:8]}",
            "local_message_id": str(uuid.uuid4()),
            "bot_id": bot_id,
        }

    # ------------------------------------------------------------------
    # Chat completion (main entry point)
    # ------------------------------------------------------------------

    async def chat_completion(self, request_data: Dict[str, Any]):
        """Handle an OpenAI-compatible chat completion request."""
        is_stream = request_data.get("stream", True)
        if is_stream:
            return StreamingResponse(
                self._stream_generator(request_data), media_type="text/event-stream"
            )
        return await self._non_stream_completion(request_data)

    async def _non_stream_completion(self, request_data: Dict[str, Any]) -> JSONResponse:
        messages = request_data.get("messages", [])
        user_model = request_data.get("model", self.config.default_model)

        bot_id = self.config.model_mapping.get(user_model)
        if not bot_id:
            raise HTTPException(status_code=400, detail=f"Unsupported model: {user_model}")

        request_id = f"chatcmpl-{uuid.uuid4()}"
        session_id = self._pick_session_id()

        try:
            payload = self._build_payload(messages, bot_id)
            logger.info("Non-stream request (model=%s)", user_model)

            result = await self.http_client.chat_completion(payload, session_id)

            if result.get("error"):
                logger.error("Chat error: %s", result["error"])
                return JSONResponse(
                    status_code=500,
                    content={
                        "error": {
                            "message": result["error"],
                            "type": "server_error",
                            "code": None,
                        }
                    },
                )

            content = result.get("content", "")
            conversation_id = result.get("conversation_id")

            # Fire-and-forget thread cleanup
            if conversation_id:
                asyncio.create_task(self.http_client.delete_thread(conversation_id, session_id))

            return JSONResponse(
                content={
                    "id": request_id,
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": user_model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                }
            )
        except HTTPException:
            raise
        except Exception as e:
            logger.error("Non-stream error: %s", e, exc_info=True)
            return JSONResponse(
                status_code=500,
                content={
                    "error": {
                        "message": f"Internal error: {e}",
                        "type": "server_error",
                        "code": None,
                    }
                },
            )

    async def _stream_generator(self, request_data: Dict[str, Any]) -> AsyncGenerator[bytes, None]:
        """Real SSE streaming — forward Doubao events as OpenAI chunks in real-time."""
        messages = request_data.get("messages", [])
        user_model = request_data.get("model", self.config.default_model)

        bot_id = self.config.model_mapping.get(user_model)
        if not bot_id:
            chunk = create_chat_completion_chunk(
                f"chatcmpl-{uuid.uuid4()}", user_model, f"Unsupported model: {user_model}", "stop"
            )
            yield create_sse_data(chunk)
            yield DONE_CHUNK
            return

        request_id = f"chatcmpl-{uuid.uuid4()}"
        session_id = self._pick_session_id()
        conversation_id: Optional[str] = None

        try:
            payload = self._build_payload(messages, bot_id)
            logger.info("Stream request (model=%s)", user_model)

            # Track the last full text we received (Doubao sends full replacement, not deltas)
            last_text = ""

            async for event in self.http_client.chat_completion_stream(payload, session_id):
                if event["type"] == "text":
                    new_text = event.get("content", "")
                    # Doubao sends cumulative text — compute the delta
                    if new_text.startswith(last_text):
                        delta = new_text[len(last_text):]
                    else:
                        delta = new_text
                    last_text = new_text

                    if delta:
                        chunk = create_chat_completion_chunk(request_id, user_model, delta)
                        yield create_sse_data(chunk)

                elif event["type"] == "done":
                    conversation_id = event.get("conversation_id")
                    break

                elif event["type"] == "error":
                    error_msg = event.get("error", "Unknown error")
                    logger.error("Stream error: %s", error_msg)
                    chunk = create_chat_completion_chunk(request_id, user_model, error_msg, "stop")
                    yield create_sse_data(chunk)
                    yield DONE_CHUNK
                    return

                elif event["type"] == "meta":
                    conversation_id = event.get("conversation_id") or conversation_id

            # Send final stop chunk
            final = create_chat_completion_chunk(request_id, user_model, "", "stop")
            yield create_sse_data(final)
            yield DONE_CHUNK

        except Exception as e:
            logger.error("Stream error: %s", e, exc_info=True)
            chunk = create_chat_completion_chunk(request_id, user_model, f"Internal error: {e}", "stop")
            yield create_sse_data(chunk)
            yield DONE_CHUNK

        # Fire-and-forget thread cleanup
        if conversation_id:
            asyncio.create_task(self.http_client.delete_thread(conversation_id, session_id))

    # ------------------------------------------------------------------
    # Model listing
    # ------------------------------------------------------------------

    async def get_models(self) -> JSONResponse:
        return JSONResponse(
            content={
                "object": "list",
                "data": [
                    {
                        "id": name,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "doubao",
                    }
                    for name in self.config.model_mapping
                ],
            }
        )
