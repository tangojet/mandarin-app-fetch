"""LLM service configuration — loaded from environment variables.

Uses CDP (Chrome DevTools Protocol) to call Doubao's API through a logged-in
browser session. The browser's Argus SDK handles request signing automatically.

Requires a Docker container with Chrome logged into doubao.com.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger("llm_service.config")


@dataclass
class LLMConfig:
    # Security
    api_key: Optional[str] = None

    # CDP: Docker container with logged-in Chrome on doubao.com
    cdp_container: str = "test-two-browser"

    # Timeouts
    request_timeout: int = 60

    # Model mapping — friendly name -> Doubao bot ID
    default_model: str = "doubao-pro-chat"
    model_mapping: Dict[str, str] = field(default_factory=lambda: {
        "doubao-pro-chat": "7338286299411103781",
    })


def load_config() -> LLMConfig:
    """Build an LLMConfig from env vars."""
    # Model mapping
    model_mapping = {"doubao-pro-chat": "7338286299411103781"}
    raw = os.environ.get("DOUBAO_MODEL_MAPPING")
    if raw:
        import json
        try:
            model_mapping = json.loads(raw)
        except Exception:
            logger.warning("DOUBAO_MODEL_MAPPING is not valid JSON, using defaults")

    return LLMConfig(
        api_key=os.environ.get("LLM_API_KEY") or os.environ.get("DOUBAO_API_KEY") or os.environ.get("API_MASTER_KEY"),
        cdp_container=os.environ.get("LLM_CDP_CONTAINER", "test-two-browser"),
        request_timeout=int(os.environ.get("LLM_REQUEST_TIMEOUT", "60")),
        default_model=os.environ.get("DOUBAO_DEFAULT_MODEL", "doubao-pro-chat"),
        model_mapping=model_mapping,
    )


def is_configured(cfg: Optional[LLMConfig] = None) -> bool:
    """Return True if CDP container is configured."""
    if cfg is None:
        cfg = load_config()
    return bool(cfg.cdp_container)
