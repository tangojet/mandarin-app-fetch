"""LLM service configuration — loaded from environment variables + cookie_manager.

Session ID loading priority:
1. LLM_SESSION_IDS env var (comma-separated sessionid values)
2. Extract sessionid from cookie_manager's stored doubao cookies
3. Extract sessionid from DOUBAO_COOKIE_1 env var (legacy compat)

If no session IDs are found, is_configured() returns False and the provider
won't be initialised. The rest of mandarin-app-fetch runs normally.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("llm_service.config")


@dataclass
class LLMConfig:
    # Security
    api_key: Optional[str] = None

    # Session IDs (extracted from cookies or env)
    session_ids: List[str] = field(default_factory=list)

    # Device params (needed for URL query)
    device_id: Optional[str] = None
    web_id: Optional[str] = None

    # Timeouts
    request_timeout: int = 180

    # Model mapping — friendly name -> Doubao bot ID
    default_model: str = "doubao-pro-chat"
    model_mapping: Dict[str, str] = field(default_factory=lambda: {
        "doubao-pro-chat": "7338286299411103781",
    })


def _extract_session_id(cookie_header: str) -> Optional[str]:
    """Pull sessionid value from a full cookie header string."""
    match = re.search(r'(?:^|;\s*)sessionid=([^;]+)', cookie_header)
    if match:
        value = match.group(1).strip()
        if value and value != "sessionid_ss":
            return value
    return None


def load_config() -> LLMConfig:
    """Build an LLMConfig from env vars + cookie_manager."""
    import cookie_manager

    session_ids: List[str] = []

    # Priority 1: LLM_SESSION_IDS env var
    raw_ids = os.environ.get("LLM_SESSION_IDS", "").strip()
    if raw_ids:
        for sid in raw_ids.split(","):
            sid = sid.strip()
            if sid:
                session_ids.append(sid)

    # Priority 2: Extract from cookie_manager's doubao cookies
    if not session_ids:
        cm_header = cookie_manager.load_cookies_as_header("doubao")
        if cm_header:
            sid = _extract_session_id(cm_header)
            if sid:
                session_ids.append(sid)

    # Priority 3: Extract from DOUBAO_COOKIE_1 env var (legacy)
    if not session_ids:
        legacy = os.environ.get("DOUBAO_COOKIE_1", "").strip()
        if legacy:
            sid = _extract_session_id(legacy)
            if sid:
                session_ids.append(sid)

    if session_ids:
        logger.info("Loaded %d session ID(s)", len(session_ids))
    else:
        logger.info("No session IDs found")

    # Device params with fallbacks
    device_id = os.environ.get("LLM_DEVICE_ID") or os.environ.get("DOUBAO_DEVICE_ID")
    web_id = os.environ.get("LLM_WEB_ID") or os.environ.get("DOUBAO_WEB_ID")

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
        session_ids=session_ids,
        device_id=device_id,
        web_id=web_id,
        request_timeout=int(os.environ.get("LLM_REQUEST_TIMEOUT", os.environ.get("DOUBAO_REQUEST_TIMEOUT", "180"))),
        default_model=os.environ.get("DOUBAO_DEFAULT_MODEL", "doubao-pro-chat"),
        model_mapping=model_mapping,
    )


def is_configured(cfg: Optional[LLMConfig] = None) -> bool:
    """Return True if the minimum required config is present."""
    if cfg is None:
        cfg = load_config()
    return bool(cfg.session_ids and cfg.device_id and cfg.web_id)
