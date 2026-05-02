"""LLM service — direct HTTP Doubao chat completions (no browser needed).

Uses sessionid cookies + Chrome header spoofing to call Doubao's
/samantha/chat/completion endpoint directly. Replaces the old Playwright-based
doubao_service with a faster, more reliable approach.
"""

from llm_service.provider import LLMProvider

__all__ = ["LLMProvider"]
