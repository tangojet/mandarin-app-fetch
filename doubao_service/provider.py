"""Doubao chat completion provider — OpenAI-compatible interface.

This is the core logic ported from doubao-2api's ``DoubaoProvider``.
It builds Doubao v2 API payloads, sends them via the PlaywrightManager's
in-browser fetch (auto-signed by Argus), and formats responses as
OpenAI-compatible JSON or SSE streams.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import HTTPException
from fastapi.responses import JSONResponse, StreamingResponse

from doubao_service.config import DoubaoConfig
from doubao_service.playwright_mgr import PlaywrightManager
from doubao_service.sessions import SessionManager
from doubao_service.sse_utils import DONE_CHUNK, create_chat_completion_chunk, create_sse_data

logger = logging.getLogger("doubao_service.provider")


class DoubaoProvider:
    def __init__(self, config: DoubaoConfig):
        self.config = config
        self.session_manager = SessionManager(ttl=config.session_cache_ttl)
        self.playwright_manager = PlaywrightManager()

    async def initialize(self) -> None:
        await self.playwright_manager.initialize(self.config, self.config.cookies)

    async def close(self) -> None:
        await self.playwright_manager.close()

    async def reload_cookies(self, cookie_str: str) -> None:
        """Hot-reload Doubao cookies into the live browser session."""
        await self.playwright_manager.reload_cookies(cookie_str)

    # ------------------------------------------------------------------
    # Payload building
    # ------------------------------------------------------------------

    def _build_payload(
        self, messages: List[Dict[str, Any]], bot_id: str, conversation_id: str
    ) -> Dict[str, Any]:
        last_user_msg = next((m for m in reversed(messages) if m.get("role") == "user"), None)
        if not last_user_msg:
            raise HTTPException(status_code=400, detail="No user message found.")

        text = last_user_msg["content"]
        now_ms = int(time.time() * 1000)
        now_sec = int(time.time())
        is_new = not conversation_id or conversation_id == "0"

        return {
            "client_meta": {
                "local_conversation_id": f"local_{uuid.uuid4().hex[:16]}",
                "conversation_id": "" if is_new else conversation_id,
                "bot_id": bot_id,
                "last_section_id": "",
                "last_message_index": None,
            },
            "messages": [
                {
                    "local_message_id": str(uuid.uuid4()),
                    "content_block": [
                        {
                            "block_type": 10000,
                            "content": {
                                "text_block": {
                                    "text": text,
                                    "icon_url": "",
                                    "icon_url_dark": "",
                                    "summary": "",
                                },
                                "pc_event_block": "",
                            },
                            "block_id": str(uuid.uuid4()),
                            "parent_id": "",
                            "meta_info": [],
                            "append_fields": [],
                        }
                    ],
                    "message_status": 0,
                }
            ],
            "option": {
                "send_message_scene": "",
                "create_time_ms": now_ms,
                "collect_id": "",
                "is_audio": False,
                "answer_with_suggest": False,
                "tts_switch": False,
                "need_deep_think": 0,
                "click_clear_context": False,
                "from_suggest": False,
                "is_regen": False,
                "is_replace": False,
                "disable_sse_cache": False,
                "select_text_action": "",
                "resend_for_regen": False,
                "scene_type": 0,
                "unique_key": str(uuid.uuid4()),
                "start_seq": 0,
                "need_create_conversation": is_new,
                "conversation_init_option": {"need_ack_conversation": True},
                "regen_query_id": [],
                "edit_query_id": [],
                "regen_instruction": "",
                "no_replace_for_regen": False,
                "message_from": 0,
                "shared_app_name": "",
                "shared_app_id": "",
                "sse_recv_event_options": {"support_chunk_delta": True},
                "is_ai_playground": False,
                "recovery_option": {
                    "is_recovery": False,
                    "req_create_time_sec": now_sec,
                    "append_sse_event_scene": 0,
                },
            },
            "ext": {
                "use_deep_think": "0",
                "fp": self.config.fp or "",
                "conversation_init_option": '{"need_ack_conversation":true}',
                "commerce_credit_config_enable": "0",
                "sub_conv_firstmet_type": "1",
            },
        }

    # ------------------------------------------------------------------
    # Chat completion (main entry point)
    # ------------------------------------------------------------------

    async def chat_completion(self, request_data: Dict[str, Any]):
        """Handle an OpenAI-compatible chat completion request.

        Returns a ``StreamingResponse`` or ``JSONResponse`` depending on
        the ``stream`` flag in *request_data*.
        """
        is_stream = request_data.get("stream", True)
        if is_stream:
            return StreamingResponse(
                self._stream_generator(request_data), media_type="text/event-stream"
            )
        return await self._non_stream_completion(request_data)

    async def _non_stream_completion(self, request_data: Dict[str, Any]) -> JSONResponse:
        session_id = request_data.get("user", f"session-{uuid.uuid4().hex}")
        messages = request_data.get("messages", [])
        user_model = request_data.get("model", self.config.default_model)

        bot_id = self.config.model_mapping.get(user_model)
        if not bot_id:
            raise HTTPException(status_code=400, detail=f"Unsupported model: {user_model}")

        session_data = self.session_manager.get_session(session_id) or {}
        conversation_id = session_data.get("conversation_id", "0")
        request_id = f"chatcmpl-{uuid.uuid4()}"

        try:
            payload = self._build_payload(messages, bot_id, conversation_id)
            logger.info("Sending non-stream request (model=%s)", user_model)

            result = await self.playwright_manager.browser_fetch_chat(payload)

            if result.get("error"):
                logger.error("Browser fetch error: %s", result["error"])
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
            new_cid = result.get("conversation_id")
            if new_cid and (not conversation_id or conversation_id == "0"):
                self.session_manager.update_session(session_id, {"conversation_id": new_cid})
                logger.info("Saved conversation_id=%s for session %s", new_cid, session_id)

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
        session_id = request_data.get("user", f"session-{uuid.uuid4().hex}")
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

        session_data = self.session_manager.get_session(session_id) or {}
        conversation_id = session_data.get("conversation_id", "0")
        request_id = f"chatcmpl-{uuid.uuid4()}"

        try:
            payload = self._build_payload(messages, bot_id, conversation_id)
            logger.info("Sending stream request (model=%s)", user_model)

            result = await self.playwright_manager.browser_fetch_chat(payload)

            if result.get("error"):
                logger.error("Browser fetch error: %s", result["error"])
                chunk = create_chat_completion_chunk(request_id, user_model, result["error"], "stop")
                yield create_sse_data(chunk)
                yield DONE_CHUNK
                return

            content = result.get("content", "")
            new_cid = result.get("conversation_id")
            if new_cid and (not conversation_id or conversation_id == "0"):
                self.session_manager.update_session(session_id, {"conversation_id": new_cid})

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
