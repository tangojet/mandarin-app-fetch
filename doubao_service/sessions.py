"""Conversation session cache — maps session IDs to Doubao conversation IDs."""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from cachetools import TTLCache

logger = logging.getLogger("doubao_service.sessions")


class SessionManager:
    def __init__(self, ttl: int = 3600, maxsize: int = 1024):
        self._cache: TTLCache = TTLCache(maxsize=maxsize, ttl=ttl)
        self._lock = threading.Lock()
        logger.info("SessionManager initialized (TTL=%ds)", ttl)

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._cache.get(session_id)

    def update_session(self, session_id: str, data: Dict[str, Any]) -> None:
        with self._lock:
            self._cache[session_id] = data
