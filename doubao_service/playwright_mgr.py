"""Playwright browser manager for Doubao — handles browser lifecycle, cookie
injection, and in-page fetch with automatic a_bogus signing via Argus SDK.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Dict, List, Optional
from urllib.parse import urlencode

from playwright.async_api import async_playwright, ConsoleMessage, TimeoutError as PwTimeout
from playwright_stealth import stealth_async

from doubao_service.config import DoubaoConfig

logger = logging.getLogger("doubao_service.playwright")


def _handle_console(msg: ConsoleMessage) -> None:
    text = msg.text
    noise = [
        "Failed to load resource", "net::ERR_FAILED", "WebSocket connection",
        "Content Security Policy", "Scripts may close only", "Ignoring too frequent",
    ]
    if any(k in text for k in noise):
        return
    lvl = msg.type.upper()
    if lvl == "ERROR":
        logger.error("[Browser] %s", text)
    elif lvl == "WARNING":
        logger.warning("[Browser] %s", text)


class PlaywrightManager:
    """Singleton managing a single headless Chromium page on doubao.com."""

    _instance: Optional["PlaywrightManager"] = None
    _lock = asyncio.Lock()

    def __new__(cls) -> "PlaywrightManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False  # type: ignore[attr-defined]
        return cls._instance

    async def initialize(self, config: DoubaoConfig, cookies: List[str]) -> None:
        if self._initialized:
            return
        async with self._lock:
            if self._initialized:
                return

            logger.info("Initializing Playwright manager (browser-fetch mode)…")
            self._config = config
            self.playwright = await async_playwright().start()
            self.browser = await self.playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox"],
            )
            self.page = await self.browser.new_page()
            await stealth_async(self.page)
            self.page.on("console", _handle_console)
            await self.page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
            )

            self._fingerprint = {
                "device_id": config.device_id,
                "fp": config.fp,
                "web_id": config.web_id,
                "tea_uuid": config.tea_uuid,
            }
            logger.info("Device fingerprint loaded: device_id=%s", config.device_id)

            if not cookies:
                raise ValueError("At least one valid cookie is required.")

            cookie_str = cookies[0]
            try:
                cookie_list = [
                    {
                        "name": c.split("=")[0].strip(),
                        "value": c.split("=", 1)[1].strip(),
                        "domain": ".doubao.com",
                        "path": "/",
                    }
                    for c in cookie_str.split(";")
                    if "=" in c
                ]
                await self.page.context.add_cookies(cookie_list)
                logger.info("Cookies injected into browser context.")
            except Exception as e:
                raise ValueError(f"Invalid cookie format: {e}") from e

            try:
                logger.info("Navigating to doubao.com/chat/ …")
                await self.page.goto(
                    "https://www.doubao.com/chat/", wait_until="load", timeout=60000
                )
                logger.info("Page navigation complete.")
            except PwTimeout as e:
                raise RuntimeError("Cannot access doubao.com (timeout).") from e

            # Check login status
            try:
                logged_in = await self.page.evaluate("""
                    () => {
                        const text = document.body.innerText || '';
                        return !text.includes('登录') || text.includes('历史对话');
                    }
                """)
                if not logged_in:
                    logger.error("Browser session is NOT logged in!")
                    raise RuntimeError("Not logged in to doubao.com. Refresh cookies.")
                logger.info("Verified: browser session is logged in.")
            except RuntimeError:
                raise
            except Exception as e:
                logger.warning("Login check inconclusive: %s", e)

            # Wait for Argus SDK to initialize (adds a_bogus interceptor)
            await asyncio.sleep(3)

            logger.info("Playwright manager ready.")
            self._initialized = True

    def _build_base_params(self) -> Dict[str, str]:
        return {
            "aid": "497858",
            "device_id": self._fingerprint["device_id"],
            "device_platform": "web",
            "fp": self._fingerprint["fp"],
            "language": "zh",
            "pc_version": "3.17.0",
            "pkg_type": "release_version",
            "real_aid": "497858",
            "region": "",
            "samantha_web": "1",
            "sys_region": "",
            "tea_uuid": self._fingerprint["tea_uuid"],
            "use-olympus-account": "1",
            "version_code": "20800",
            "web_id": self._fingerprint["web_id"],
            "web_tab_id": str(uuid.uuid4()),
        }

    # -- Long JS executed inside the browser page context --
    _SSE_PARSER_JS = """
    async ([url, bodyStr]) => {
        try {
            const resp = await fetch(url, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: bodyStr
            });

            if (!resp.ok) {
                const errText = await resp.text();
                return {error: `HTTP ${resp.status}: ${errText.substring(0, 200)}`, content: '', conversation_id: null};
            }

            const text = await resp.text();
            const rawLines = text.split('\\n');
            let content = '';
            let conversationId = null;
            let errorMsg = null;
            let currentEvent = null;

            for (const line of rawLines) {
                const trimmed = line.trim();

                if (trimmed.startsWith('event:')) {
                    currentEvent = trimmed.substring(6).trim();
                    continue;
                }

                if (trimmed.startsWith('data:')) {
                    const dataStr = trimmed.substring(5).trim();
                    if (!dataStr || dataStr === '{}') continue;

                    try {
                        const data = JSON.parse(dataStr);

                        if (currentEvent === 'STREAM_ERROR') {
                            const code = data.error_code || 0;
                            const msg = data.error_msg || 'Unknown error';
                            errorMsg = `${code}: ${msg}`;
                            continue;
                        }

                        if (currentEvent === 'SSE_REPLY' || currentEvent === 'SSE_CHUNK') {
                            const msg = data.message || data;
                            if (msg.content_block) {
                                for (const block of msg.content_block) {
                                    const tb = block?.content?.text_block;
                                    if (tb?.text) content = tb.text;
                                }
                            }
                            if (data.text) content = data.text;
                            if (data.chunk_delta) content += data.chunk_delta;
                            if (data.conversation_id) conversationId = data.conversation_id;
                            if (msg.conversation_id) conversationId = msg.conversation_id;
                            continue;
                        }

                        if (currentEvent === 'SSE_CONVERSATION_CREATED') {
                            conversationId = data.conversation_id || null;
                            continue;
                        }

                        // Fallback: old format with event_type inside data
                        const eventType = data.event_type;
                        if (eventType !== undefined) {
                            if (eventType === 2005) {
                                const eventData = typeof data.event_data === 'string' ?
                                    JSON.parse(data.event_data) : (data.event_data || {});
                                if (eventData.code === 710022002 || eventData.code === 710022004) {
                                    errorMsg = `${eventData.code}: ${eventData.msg || 'Rate limited'}`;
                                }
                            }
                            if (eventType === 2002) {
                                const eventData = typeof data.event_data === 'string' ?
                                    JSON.parse(data.event_data) : (data.event_data || {});
                                conversationId = eventData.conversation_id || null;
                            }
                            if (eventType === 2001) {
                                const eventData = typeof data.event_data === 'string' ?
                                    JSON.parse(data.event_data) : (data.event_data || {});
                                const msg = eventData.message || {};
                                if (msg.content_block) {
                                    for (const block of msg.content_block) {
                                        const tb = block?.content?.text_block;
                                        if (tb?.text) content = tb.text;
                                    }
                                } else {
                                    try {
                                        const cj = JSON.parse(msg.content || '{}');
                                        if (cj.text) content = cj.text;
                                    } catch(e) {}
                                }
                            }
                        }

                        // Generic fallback
                        if (!content && !eventType && currentEvent !== 'SSE_HEARTBEAT' && currentEvent !== 'SSE_REPLY_END') {
                            if (data.message?.content_block) {
                                for (const block of data.message.content_block) {
                                    const tb = block?.content?.text_block;
                                    if (tb?.text) content = tb.text;
                                }
                            }
                            if (data.conversation_id && !conversationId) {
                                conversationId = data.conversation_id;
                            }
                        }
                    } catch(e) {}
                }

                if (trimmed === '') {
                    currentEvent = null;
                }
            }

            if (errorMsg && !content) {
                return {error: errorMsg, content: '', conversation_id: null};
            }

            return {error: null, content, conversation_id: conversationId};
        } catch(e) {
            return {error: e.message, content: '', conversation_id: null};
        }
    }
    """

    async def browser_fetch_chat(self, payload: Dict) -> Dict:
        """Execute a chat completion request from within the browser context.

        The Argus SDK interceptor automatically adds ``a_bogus`` to the URL.
        Returns ``{"error": ..., "content": ..., "conversation_id": ...}``.
        """
        async with self._lock:
            if not self._initialized:
                raise RuntimeError("PlaywrightManager not initialized.")

            try:
                params = self._build_base_params()
                url = f"https://www.doubao.com/chat/completion?{urlencode(params)}"
                payload_json = json.dumps(payload, ensure_ascii=False)

                logger.info("Executing browser-context fetch to /chat/completion…")
                result = await self.page.evaluate(self._SSE_PARSER_JS, [url, payload_json])

                if result.get("error"):
                    logger.error("Browser fetch error: %s", result["error"])
                else:
                    logger.info("Browser fetch OK: %d chars", len(result.get("content", "")))

                return result
            except Exception as e:
                logger.error("browser_fetch_chat error: %s", e, exc_info=True)
                return {"error": str(e), "content": "", "conversation_id": None}

    async def close(self) -> None:
        if self._initialized:
            async with self._lock:
                if self.browser:
                    await self.browser.close()
                if self.playwright:
                    await self.playwright.stop()
                self._initialized = False
                PlaywrightManager._instance = None
                logger.info("Playwright manager closed.")
