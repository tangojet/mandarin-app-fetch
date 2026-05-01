"""Base platform interface."""

from __future__ import annotations

from abc import ABC, abstractmethod

from models import SocialMediaPost


class BasePlatform(ABC):
    """Abstract base class for platform fetchers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Platform identifier, e.g. 'xhs'."""
        ...

    @abstractmethod
    async def fetch(self, url: str, max_comments: int = 10) -> SocialMediaPost:
        """Fetch a post from the given URL."""
        ...
