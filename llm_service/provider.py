"""LLM provider — OpenAI-compatible chat completions via CDP.

Each request creates a new conversation in the browser, gets the response,
and the CDP script deletes the thread automatically.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from llm_service.cdp_client import DoubaoCDPClient
from llm_service.config import LLMConfig
from llm_service.sse_utils import DONE_CHUNK, create_chat_completion_chunk, create_sse_data

logger = logging.getLogger("llm_service.provider")


class LLMProvider:
    def __init__(self, config: LLMConfig):
        self.config = config
        self.cdp_client = DoubaoCDPClient(
            container=config.cdp_container,
            timeout=config.request_timeout,
        )

    async def initialize(self) -> None:
        """Check that the browser has a doubao.com tab."""
        ok = await self.cdp_client.check_browser()
        if ok:
            logger.info("Browser has doubao.com tab — CDP ready")
        else:
            logger.warning("No doubao.com tab found in %s — chat will fail until browser is ready",
                           self.config.cdp_container)

    async def close(self) -> None:
        await self.cdp_client.close()

    def _extract_text(self, messages: list[Dict[str, Any]]) -> str:
        """Extract the last user message text."""
        last_user_msg = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if not last_user_msg:
            raise HTTPException(status_code=400, detail="No user message found.")
        return last_user_msg["content"]

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
        text = self._extract_text(messages)

        try:
            logger.info("Non-stream request (model=%s)", user_model)
            result = await self.cdp_client.chat_completion(text, bot_id)

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
        """Fake streaming — CDP collects the full response, then we emit it as chunks."""
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

        try:
            text = self._extract_text(messages)
            logger.info("Stream request (model=%s)", user_model)

            result = await self.cdp_client.chat_completion(text, bot_id)

            if result.get("error"):
                logger.error("Stream error: %s", result["error"])
                chunk = create_chat_completion_chunk(request_id, user_model, result["error"], "stop")
                yield create_sse_data(chunk)
                yield DONE_CHUNK
                return

            content = result.get("content", "")
            if content:
                chunk = create_chat_completion_chunk(request_id, user_model, content)
                yield create_sse_data(chunk)

            final = create_chat_completion_chunk(request_id, user_model, "", "stop")
            yield create_sse_data(final)
            yield DONE_CHUNK

        except Exception as e:
            logger.error("Stream error: %s", e, exc_info=True)
            chunk = create_chat_completion_chunk(request_id, user_model, f"Internal error: {e}", "stop")
            yield create_sse_data(chunk)
            yield DONE_CHUNK

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
