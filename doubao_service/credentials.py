"""Cookie credential rotation for multi-account support."""

from __future__ import annotations

import logging
import threading
from typing import List

logger = logging.getLogger("doubao_service.credentials")


class CredentialManager:
    def __init__(self, credentials: List[str]):
        if not credentials:
            raise ValueError("Credential list must not be empty.")
        self.credentials = credentials
        self.index = 0
        self._lock = threading.Lock()
        logger.info("CredentialManager initialized with %d credential(s)", len(credentials))

    def get_credential(self) -> str:
        with self._lock:
            cred = self.credentials[self.index]
            self.index = (self.index + 1) % len(self.credentials)
            return cred
