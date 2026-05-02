"""Doubao service configuration — loaded from environment variables.

Unlike the standalone doubao-2api, this does NOT fail hard when env vars are
missing.  Instead, ``is_configured()`` returns False and the provider simply
won't be initialised.  This lets the rest of mandarin-app-fetch run normally.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("doubao_service.config")


@dataclass
class DoubaoConfig:
    # Security
    api_master_key: Optional[str] = None

    # Cookies (loaded from DOUBAO_COOKIE_1, DOUBAO_COOKIE_2, …)
    cookies: List[str] = field(default_factory=list)

    # Device fingerprint
    device_id: Optional[str] = None
    fp: Optional[str] = None
    tea_uuid: Optional[str] = None
    web_id: Optional[str] = None

    # Timeouts & caching
    request_timeout: int = 180
    session_cache_ttl: int = 3600

    # Model mapping — friendly name → Doubao bot ID
    default_model: str = "doubao-pro-chat"
    model_mapping: Dict[str, str] = field(default_factory=lambda: {
        "doubao-pro-chat": "7338286299411103781",
    })


def load_config() -> DoubaoConfig:
    """Build a ``DoubaoConfig`` from environment variables.

    Cookie loading priority:
    1. cookie_manager file (~/.mandarin-app-fetch/doubao-cookies.json)
    2. DOUBAO_COOKIE_1 env var (legacy)
    """
    import cookie_manager

    # Try cookie_manager first (unified file-based storage)
    cm_header = cookie_manager.load_cookies_as_header("doubao")
    cookies: List[str] = []
    if cm_header:
        cookies = [cm_header]
    else:
        # Fall back to env vars
        i = 1
        while True:
            val = os.environ.get(f"DOUBAO_COOKIE_{i}")
            if val:
                cookies.append(val)
                i += 1
            else:
                break

    # Accept MODEL_MAPPING as JSON if provided
    model_mapping = {"doubao-pro-chat": "7338286299411103781"}
    raw = os.environ.get("DOUBAO_MODEL_MAPPING")
    if raw:
        import json
        try:
            model_mapping = json.loads(raw)
        except Exception:
            logger.warning("DOUBAO_MODEL_MAPPING is not valid JSON, using defaults")

    return DoubaoConfig(
        api_master_key=os.environ.get("DOUBAO_API_KEY") or os.environ.get("API_MASTER_KEY"),
        cookies=cookies,
        device_id=os.environ.get("DOUBAO_DEVICE_ID"),
        fp=os.environ.get("DOUBAO_FP"),
        tea_uuid=os.environ.get("DOUBAO_TEA_UUID"),
        web_id=os.environ.get("DOUBAO_WEB_ID"),
        request_timeout=int(os.environ.get("DOUBAO_REQUEST_TIMEOUT", "180")),
        session_cache_ttl=int(os.environ.get("DOUBAO_SESSION_CACHE_TTL", "3600")),
        default_model=os.environ.get("DOUBAO_DEFAULT_MODEL", "doubao-pro-chat"),
        model_mapping=model_mapping,
    )


def is_configured(cfg: Optional[DoubaoConfig] = None) -> bool:
    """Return True if the minimum required env vars are present."""
    if cfg is None:
        cfg = load_config()
    return bool(
        cfg.cookies
        and cfg.device_id
        and cfg.fp
        and cfg.tea_uuid
        and cfg.web_id
    )
