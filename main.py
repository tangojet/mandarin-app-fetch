"""Unified social media content fetch API — FastAPI app."""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Dict, List

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse

from browser_manager import close_browser, update_platform_cookies
from extractors.doubao import extract_with_doubao, is_configured as doubao_configured
from extractors.yuanbao import extract_with_yuanbao, is_configured as yuanbao_configured
from models import SocialMediaPost
from platforms.bilibili import BilibiliPlatform
from platforms.douyin import DouyinPlatform
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("media-fetch-api starting")
    yield
    logger.info("media-fetch-api shutting down")
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


@app.post("/cookies/{platform}")
async def update_cookies(platform: str, cookies: List[dict]):
    if platform not in PLATFORMS:
        raise HTTPException(status_code=400, detail=f"Unknown platform: {platform}")
    await update_platform_cookies(platform, cookies)
    return {"status": "ok", "count": len(cookies)}


# --- /extract endpoint — LLM-based content summarization ---

# Extract backend routing: platform → backend name
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
    if backend == "doubao" and not doubao_configured():
        raise HTTPException(
            status_code=501,
            detail="Doubao backend not configured (DOUBAO_API_URL not set)",
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8089)
