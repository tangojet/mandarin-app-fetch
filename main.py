"""Unified social media content fetch API — FastAPI app."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException, Header, Query, Request
from fastapi.responses import JSONResponse

import cookie_manager
from browser_manager import close_browser, update_platform_cookies
from llm_service.config import LLMConfig, is_configured as llm_is_configured, load_config as load_llm_config
from llm_service.provider import LLMProvider
from extractors.doubao import extract_with_doubao
from extractors.yuanbao import extract_with_yuanbao, is_configured as yuanbao_configured
from models import SocialMediaPost
from platforms.bilibili import BilibiliPlatform
from platforms.douyin import DouyinPlatform
from platforms.goofish import GoofishPlatform
from platforms.weibo import WeiboPlatform
from platforms.xhs import XhsPlatform
from platforms.toutiao import ToutiaoPlatform
from platforms.xueqiu import XueqiuPlatform
from url_parser import detect_platform

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("media-fetch-api")

# Platform registry
PLATFORMS = {
    "xhs": XhsPlatform(),
    "douyin": DouyinPlatform(),
    "bilibili": BilibiliPlatform(),
    "weibo": WeiboPlatform(),
    "xueqiu": XueqiuPlatform(),
    "toutiao": ToutiaoPlatform(),
    "goofish": GoofishPlatform(),
}

# Rate limiting — minimum seconds between requests per platform
# Override via env: RATE_LIMIT_XHS=15, RATE_LIMIT_BILIBILI=5, etc.
DEFAULT_RATE_LIMITS: Dict[str, int] = {
    "xhs": 10,
    "douyin": 10,
    "weibo": 8,
    "bilibili": 5,
    "xueqiu": 5,
    "toutiao": 8,
    "goofish": 8,
}
RATE_LIMITS = {
    k: int(os.environ.get(f"RATE_LIMIT_{k.upper()}", v))
    for k, v in DEFAULT_RATE_LIMITS.items()
}
_last_request: Dict[str, float] = {}


def _check_rate_limit(platform: str) -> None:
    """Raise 429 if requesting too fast for this platform."""
    limit = RATE_LIMITS.get(platform, 5)
    now = time.monotonic()
    last = _last_request.get(platform)
    if last is None:
        _last_request[platform] = now
        return
    elapsed = now - last
    if elapsed < limit:
        wait = round(limit - elapsed, 1)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited: please wait {wait}s before next {platform} request (min interval: {limit}s)",
        )
    _last_request[platform] = now


# --- LLM provider (global, initialised in lifespan) ---
_llm_provider: Optional[LLMProvider] = None
_llm_config: Optional[LLMConfig] = None


def get_llm_provider() -> Optional[LLMProvider]:
    """Return the global LLMProvider if initialised."""
    return _llm_provider


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _llm_provider, _llm_config

    logger.info("media-fetch-api starting")

    # Seed/migrate cookies before initializing providers
    cookie_manager.seed_from_env()
    cookie_manager.migrate_yuanbao_txt()

    # Initialise LLM provider if configured
    _llm_config = load_llm_config()
    if llm_is_configured(_llm_config):
        try:
            _llm_provider = LLMProvider(_llm_config)
            await _llm_provider.initialize()
            logger.info("LLM provider initialised (models: %s)",
                        ", ".join(_llm_config.model_mapping.keys()))
        except Exception:
            logger.exception("Failed to initialise LLM provider — continuing without it")
            _llm_provider = None
    else:
        logger.info("LLM provider not configured (no CDP container), skipping")

    yield

    logger.info("media-fetch-api shutting down")
    if _llm_provider:
        await _llm_provider.close()
    await close_browser()


app = FastAPI(title="media-fetch-api", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/platforms")
async def list_platforms():
    return {"platforms": list(PLATFORMS.keys())}


@app.get("/rate-limits")
async def rate_limits():
    now = time.monotonic()
    return {
        "limits": RATE_LIMITS,
        "cooldowns": {
            k: max(0, round(RATE_LIMITS.get(k, 0) - (now - _last_request[k]), 1))
            if k in _last_request else 0
            for k in PLATFORMS
        },
    }


@app.get("/fetch")
async def fetch_post(
    url: str = Query(..., description="Social media post URL"),
    max_comments: int = Query(10, ge=0, le=50, description="Max comments to fetch"),
):
    platform_name = detect_platform(url)
    if not platform_name:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported URL. Supported platforms: {', '.join(PLATFORMS.keys())}",
        )

    platform = PLATFORMS.get(platform_name)
    if not platform:
        raise HTTPException(status_code=400, detail=f"Platform '{platform_name}' not implemented")

    _check_rate_limit(platform_name)

    try:
        post: SocialMediaPost = await platform.fetch(url, max_comments=max_comments)
        return post.dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception(f"fetch error for {url}")
        raise HTTPException(status_code=500, detail=f"Fetch failed: {e}")


@app.get("/search")
async def search_posts(
    platform: str = Query(..., description="Platform to search (currently: xueqiu)"),
    q: str = Query(..., description="Search query"),
    count: int = Query(5, ge=1, le=20, description="Number of results"),
):
    plat = PLATFORMS.get(platform)
    if not plat:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")

    if not hasattr(plat, "search"):
        raise HTTPException(status_code=400, detail=f"Platform '{platform}' does not support search")

    _check_rate_limit(platform)

    try:
        results = await plat.search(q, count=count)
        return {"platform": platform, "query": q, "count": len(results), "results": results}
    except Exception as e:
        logger.exception(f"search error for {platform}: {q}")
        raise HTTPException(status_code=500, detail=f"Search failed: {e}")


@app.post("/cookies/{service}")
async def update_cookies(service: str, request: Request):
    """Update cookies for any service.

    Accepts:
    - Content-Type: text/plain -> raw cookie header string ("k1=v1; k2=v2")
    - Content-Type: application/json -> Playwright JSON array or raw string
    """
    all_services = set(cookie_manager.SERVICES) | set(PLATFORMS)
    if service not in all_services:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown service: {service}. Known: {sorted(all_services)}",
        )
    if service in cookie_manager.CDP_SERVICES:
        raise HTTPException(
            status_code=400,
            detail=f"{service} uses Docker CDP — manage cookies via the Chrome session, not this API.",
        )

    content_type = request.headers.get("content-type", "")
    if "text/plain" in content_type:
        body = (await request.body()).decode("utf-8").strip()
        cookies = cookie_manager.detect_and_normalize(body, service)
    else:
        body = await request.json()
        cookies = cookie_manager.detect_and_normalize(body, service)

    # Hot-reload into live system (update_platform_cookies saves + reloads context)
    if service in PLATFORMS:
        await update_platform_cookies(service, cookies)
    else:
        cookie_manager.save_cookies(service, cookies, source="api")

    # doubao: CDP uses browser's live cookies — file not used at runtime, just saved for backup
    # yuanbao: reads file fresh each request — no-op

    return {"status": "ok", "count": len(cookies)}


@app.get("/cookies/status")
async def cookies_status():
    """Return cookie status for all known services."""
    return cookie_manager.get_all_status()


# --- /extract endpoint — LLM-based content summarization ---

# Extract backend routing: platform -> backend name
EXTRACT_ROUTING: Dict[str, str] = {
    "wechat": "yuanbao",
}
# All other platforms default to "doubao"
EXTRACT_DEFAULT_BACKEND = "doubao"

# Rate limiting for extract (reuse the same mechanism)
EXTRACT_RATE_LIMITS: Dict[str, int] = {
    "doubao": int(os.environ.get("RATE_LIMIT_EXTRACT_DOUBAO", "5")),
    "yuanbao": int(os.environ.get("RATE_LIMIT_EXTRACT_YUANBAO", "5")),
}
_last_extract_request: Dict[str, float] = {}


def _check_extract_rate_limit(backend: str) -> None:
    """Raise 429 if requesting too fast for this extract backend."""
    limit = EXTRACT_RATE_LIMITS.get(backend, 5)
    now = time.monotonic()
    last = _last_extract_request.get(backend)
    if last is None:
        _last_extract_request[backend] = now
        return
    elapsed = now - last
    if elapsed < limit:
        wait = round(limit - elapsed, 1)
        raise HTTPException(
            status_code=429,
            detail=f"Rate limited: please wait {wait}s before next {backend} extract request (min interval: {limit}s)",
        )
    _last_extract_request[backend] = now


@app.get("/extract")
async def extract_content(
    url: str = Query(..., description="URL to summarize via LLM"),
):
    platform = detect_platform(url)
    backend = EXTRACT_ROUTING.get(platform, EXTRACT_DEFAULT_BACKEND) if platform else EXTRACT_DEFAULT_BACKEND

    # Check backend availability
    if backend == "yuanbao" and not yuanbao_configured():
        raise HTTPException(
            status_code=501,
            detail="Yuanbao backend not configured (missing cookie file)",
        )
    if backend == "doubao" and not _llm_provider:
        raise HTTPException(
            status_code=501,
            detail="LLM backend not configured (no CDP container)",
        )

    _check_extract_rate_limit(backend)

    try:
        if backend == "yuanbao":
            summary = await extract_with_yuanbao(url)
        else:
            summary = await extract_with_doubao(url)
    except Exception as e:
        logger.exception(f"extract error for {url}")
        raise HTTPException(status_code=500, detail=f"Extract failed: {e}")

    if not summary:
        raise HTTPException(status_code=502, detail=f"Backend {backend} returned no content")

    return {
        "platform": platform or "unknown",
        "summary": summary,
        "backend": backend,
        "url": url,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
    }


# --- LLM OpenAI-compatible endpoints ---

async def _verify_llm_api_key(authorization: Optional[str] = Header(None)) -> None:
    """Check Bearer token against LLM_API_KEY / DOUBAO_API_KEY if set."""
    if not _llm_config or not _llm_config.api_key:
        return
    key = _llm_config.api_key
    if key == "1":  # magic value = no auth
        return
    if not authorization or "bearer" not in authorization.lower():
        raise HTTPException(status_code=401, detail="Bearer token required.")
    token = authorization.split(" ")[-1]
    if token != key:
        raise HTTPException(status_code=403, detail="Invalid API key.")


@app.post("/v1/chat/completions")
async def llm_chat_completions(request: Request, authorization: Optional[str] = Header(None)):
    await _verify_llm_api_key(authorization)
    if not _llm_provider:
        raise HTTPException(status_code=501, detail="LLM provider not configured or failed to initialise.")
    try:
        request_data = await request.json()
        logger.info("POST /v1/chat/completions model=%s stream=%s",
                     request_data.get("model", "?"), request_data.get("stream", True))
        return await _llm_provider.chat_completion(request_data)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error in /v1/chat/completions")
        raise HTTPException(status_code=500, detail=f"Internal error: {e}")


@app.get("/v1/models")
async def llm_list_models(authorization: Optional[str] = Header(None)):
    await _verify_llm_api_key(authorization)
    if not _llm_provider:
        raise HTTPException(status_code=501, detail="LLM provider not configured or failed to initialise.")
    return await _llm_provider.get_models()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8089)
