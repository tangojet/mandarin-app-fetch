"""SSE formatting helpers for OpenAI-compatible streaming responses."""

from __future__ import annotations

import json
import time
from typing import Any, Dict, Optional

DONE_CHUNK = b"data: [DONE]\n\n"


def create_sse_data(data: Dict[str, Any]) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode("utf-8")


def create_chat_completion_chunk(
    request_id: str,
    model: str,
    content: str,
    finish_reason: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": request_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"content": content},
                "finish_reason": finish_reason,
            }
        ],
    }
