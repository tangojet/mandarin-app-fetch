"""LLM service — Doubao chat completions via CDP (Chrome DevTools Protocol).

Executes chat requests inside a logged-in Docker Chrome session on doubao.com.
The browser's Argus SDK handles a_bogus/msToken signing automatically.
"""

from llm_service.provider import LLMProvider

__all__ = ["LLMProvider"]
