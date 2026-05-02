"""Doubao (豆包) chat completion service — integrated from doubao-2api.

Provides an OpenAI-compatible chat completions interface backed by Doubao's
web API, using Playwright for in-browser fetch with automatic a_bogus signing.
"""

from doubao_service.provider import DoubaoProvider

__all__ = ["DoubaoProvider"]
