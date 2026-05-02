"""Playwright singleton browser lifecycle management."""

from __future__ import annotations

import logging
from typing import Optional

from playwright.async_api import Browser, BrowserContext, async_playwright

import cookie_manager

logger = logging.getLogger("media-fetch-api")

_pw = None
_browser: Optional[Browser] = None
_xhs_context: Optional[BrowserContext] = None
_douyin_context: Optional[BrowserContext] = None
_bilibili_context: Optional[BrowserContext] = None
_weibo_context: Optional[BrowserContext] = None
_xueqiu_context: Optional[BrowserContext] = None
_toutiao_context: Optional[BrowserContext] = None


async def get_browser() -> Browser:
    """Get or launch headless Chromium."""
    global _pw, _browser
    if _browser and _browser.is_connected():
        return _browser
    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(
        headless=True,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
    )
    logger.info("browser launched")
    return _browser


async def get_xhs_context() -> BrowserContext:
    """Get or create a browser context for XHS with cookies loaded."""
    global _xhs_context
    if _xhs_context:
        try:
            # Check if context is still alive
            _xhs_context.pages  # noqa: B018
            return _xhs_context
        except Exception:
            _xhs_context = None

    browser = await get_browser()
    _xhs_context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )

    # Stealth: override navigator.webdriver
    await _xhs_context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    # Load cookies if available
    cookies = cookie_manager.load_cookies("xhs")
    if cookies:
        try:
            await _xhs_context.add_cookies(cookies)
            logger.info(f"loaded {len(cookies)} XHS cookies")
        except Exception as e:
            logger.warning(f"failed to load XHS cookies: {e}")

    return _xhs_context


async def get_douyin_context() -> BrowserContext:
    """Get or create a browser context for Douyin with desktop UA and cookies."""
    global _douyin_context
    if _douyin_context:
        try:
            _douyin_context.pages  # noqa: B018
            return _douyin_context
        except Exception:
            _douyin_context = None

    browser = await get_browser()
    _douyin_context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    await _douyin_context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    cookies = cookie_manager.load_cookies("douyin")
    if cookies:
        try:
            await _douyin_context.add_cookies(cookies)
            logger.info(f"loaded {len(cookies)} Douyin cookies")
        except Exception as e:
            logger.warning(f"failed to load Douyin cookies: {e}")

    return _douyin_context


async def get_bilibili_context() -> BrowserContext:
    """Get or create a browser context for Bilibili with desktop UA and cookies."""
    global _bilibili_context
    if _bilibili_context:
        try:
            _bilibili_context.pages  # noqa: B018
            return _bilibili_context
        except Exception:
            _bilibili_context = None

    browser = await get_browser()
    _bilibili_context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    await _bilibili_context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    cookies = cookie_manager.load_cookies("bilibili")
    if cookies:
        try:
            await _bilibili_context.add_cookies(cookies)
            logger.info(f"loaded {len(cookies)} Bilibili cookies")
        except Exception as e:
            logger.warning(f"failed to load Bilibili cookies: {e}")

    return _bilibili_context


async def get_weibo_context() -> BrowserContext:
    """Get or create a browser context for Weibo with mobile UA and cookies."""
    global _weibo_context
    if _weibo_context:
        try:
            _weibo_context.pages  # noqa: B018
            return _weibo_context
        except Exception:
            _weibo_context = None

    browser = await get_browser()
    _weibo_context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) "
            "Version/17.0 Mobile/15E148 Safari/604.1"
        ),
        viewport={"width": 390, "height": 844},
    )
    await _weibo_context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    cookies = cookie_manager.load_cookies("weibo")
    if cookies:
        try:
            await _weibo_context.add_cookies(cookies)
            logger.info(f"loaded {len(cookies)} Weibo cookies")
        except Exception as e:
            logger.warning(f"failed to load Weibo cookies: {e}")

    return _weibo_context


async def get_xueqiu_context() -> BrowserContext:
    """Get or create a browser context for Xueqiu with desktop UA and cookies."""
    global _xueqiu_context
    if _xueqiu_context:
        try:
            _xueqiu_context.pages  # noqa: B018
            return _xueqiu_context
        except Exception:
            _xueqiu_context = None

    browser = await get_browser()
    _xueqiu_context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    await _xueqiu_context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    cookies = cookie_manager.load_cookies("xueqiu")
    if cookies:
        try:
            await _xueqiu_context.add_cookies(cookies)
            logger.info(f"loaded {len(cookies)} Xueqiu cookies")
        except Exception as e:
            logger.warning(f"failed to load Xueqiu cookies: {e}")

    return _xueqiu_context


async def get_toutiao_context() -> BrowserContext:
    """Get or create a browser context for Toutiao with desktop UA and cookies."""
    global _toutiao_context
    if _toutiao_context:
        try:
            _toutiao_context.pages  # noqa: B018
            return _toutiao_context
        except Exception:
            _toutiao_context = None

    browser = await get_browser()
    _toutiao_context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1280, "height": 800},
    )
    await _toutiao_context.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )

    cookies = cookie_manager.load_cookies("toutiao")
    if cookies:
        try:
            await _toutiao_context.add_cookies(cookies)
            logger.info(f"loaded {len(cookies)} Toutiao cookies")
        except Exception as e:
            logger.warning(f"failed to load Toutiao cookies: {e}")

    return _toutiao_context


async def update_xhs_cookies(cookies: list) -> None:
    """Update XHS cookies on disk and in the live context."""
    await update_platform_cookies("xhs", cookies)


async def update_platform_cookies(platform: str, cookies: list) -> None:
    """Update cookies for any platform on disk and in the live context."""
    cookie_manager.save_cookies(platform, cookies, source="api")

    # Reload into live context if it exists
    context_map = {
        "xhs": "_xhs_context",
        "douyin": "_douyin_context",
        "bilibili": "_bilibili_context",
        "weibo": "_weibo_context",
        "xueqiu": "_xueqiu_context",
        "toutiao": "_toutiao_context",
    }
    ctx_name = context_map.get(platform)
    if ctx_name:
        ctx = globals().get(ctx_name)
        if ctx:
            try:
                await ctx.add_cookies(cookies)
            except Exception as e:
                logger.warning(f"failed to reload {platform} cookies into context: {e}")


async def close_browser() -> None:
    """Shut down browser and contexts."""
    global _pw, _browser, _xhs_context, _douyin_context, _bilibili_context, _weibo_context, _xueqiu_context, _toutiao_context
    for ctx, name in [
        (_xhs_context, "xhs"),
        (_douyin_context, "douyin"),
        (_bilibili_context, "bilibili"),
        (_weibo_context, "weibo"),
        (_xueqiu_context, "xueqiu"),
        (_toutiao_context, "toutiao"),
    ]:
        if ctx:
            try:
                await ctx.close()
            except Exception:
                pass
    _xhs_context = None
    _douyin_context = None
    _bilibili_context = None
    _weibo_context = None
    _xueqiu_context = None
    _toutiao_context = None
    if _browser:
        await _browser.close()
        _browser = None
    if _pw:
        await _pw.stop()
        _pw = None
    logger.info("browser closed")
