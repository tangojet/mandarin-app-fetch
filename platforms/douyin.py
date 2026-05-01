"""Douyin (抖音) fetcher — Playwright primary, Docker browser CDP fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import List, Optional

from browser_manager import get_douyin_context
from models import Author, Comment, MusicInfo, SocialMediaPost
from platforms.base import BasePlatform
from url_parser import extract_douyin_aweme_id

logger = logging.getLogger("media-fetch-api")

DOUYIN_SHORTLINK_PREFIX = "v.douyin.com"

# Docker container with a logged-in Chrome browser for Douyin
DOUYIN_DOCKER_CONTAINER = os.environ.get("DOUYIN_DOCKER_CONTAINER", "test-two-browser")
DOUYIN_CDP_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "douyin-cdp-extract.js")


def _find_docker() -> Optional[str]:
    """Find the docker binary path."""
    docker = shutil.which("docker")
    if docker:
        return docker
    for p in ["/Applications/Docker.app/Contents/Resources/bin/docker", "/usr/local/bin/docker"]:
        if os.path.isfile(p):
            return p
    return None


class DouyinPlatform(BasePlatform):
    @property
    def name(self) -> str:
        return "douyin"

    def __init__(self):
        self._docker_bin = _find_docker()
        self._cdp_script_copied = False

    async def _ensure_cdp_script(self):
        """Copy the CDP extract script into the Docker container."""
        if self._cdp_script_copied or not self._docker_bin:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker_bin, "cp", DOUYIN_CDP_SCRIPT,
                f"{DOUYIN_DOCKER_CONTAINER}:/tmp/douyin-cdp-extract.js",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode == 0:
                self._cdp_script_copied = True
                logger.info("douyin: CDP extract script copied to Docker container")
        except Exception as e:
            logger.warning(f"douyin: failed to copy CDP script: {e}")

    async def _fetch_via_docker(self, aweme_id: str, max_comments: int) -> Optional[SocialMediaPost]:
        """Fallback: fetch using Docker browser's CDP. Returns None if unavailable."""
        if not self._docker_bin:
            return None

        await self._ensure_cdp_script()
        if not self._cdp_script_copied:
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker_bin, "exec", DOUYIN_DOCKER_CONTAINER,
                "node", "/tmp/douyin-cdp-extract.js", aweme_id, str(max_comments),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=40)

            if stderr:
                logger.info(f"douyin-cdp: {stderr.decode().strip()}")

            if proc.returncode != 0:
                logger.warning(f"douyin-cdp: exited {proc.returncode}")
                return None

            data = json.loads(stdout.decode())
            if data.get("error"):
                logger.warning(f"douyin-cdp: {data['error']}")
                return None

            # Parse music
            music = None
            if data.get("music"):
                music = MusicInfo(
                    title=data["music"].get("title", ""),
                    author=data["music"].get("author", ""),
                    duration=data["music"].get("duration", 0),
                )

            return SocialMediaPost(
                platform=data["platform"],
                title=data["title"],
                author=Author(
                    name=data["author"]["name"],
                    id=data["author"].get("id", ""),
                    avatar=data["author"].get("avatar"),
                    signature=data["author"].get("signature"),
                    ip_location=data["author"].get("ip_location"),
                ),
                content=data["content"],
                stats=data["stats"],
                images=data.get("images", []),
                video_url=data.get("video_url"),
                music=music,
                create_time=data.get("create_time"),
                aweme_type=data.get("aweme_type"),
                comments=[Comment(**c) for c in data.get("comments", [])],
                url=data["url"],
                fetched_at=data["fetched_at"],
            )
        except asyncio.TimeoutError:
            logger.warning("douyin-cdp: timed out")
            return None
        except Exception as e:
            logger.warning(f"douyin-cdp: {e}")
            return None

    async def fetch(self, url: str, max_comments: int = 10) -> SocialMediaPost:
        ctx = await get_douyin_context()

        # Resolve short links
        if DOUYIN_SHORTLINK_PREFIX in url:
            url = await self._resolve_short_url(ctx, url)
            logger.info(f"douyin: resolved short URL to {url}")

        aweme_id = extract_douyin_aweme_id(url)
        if not aweme_id:
            raise ValueError(f"Could not extract aweme_id from URL: {url}")

        # Try Playwright first
        try:
            return await self._fetch_via_playwright(ctx, url, aweme_id, max_comments)
        except Exception as e:
            logger.warning(f"douyin: Playwright fetch failed: {e}, trying CDP fallback")

        # Fallback: CDP via Docker browser
        cdp_result = await self._fetch_via_docker(aweme_id, max_comments)
        if cdp_result:
            cdp_result.url = url  # preserve original URL
            return cdp_result

        raise RuntimeError(f"Failed to fetch Douyin post {aweme_id} via both Playwright and CDP")

    async def _fetch_via_playwright(
        self, ctx, url: str, aweme_id: str, max_comments: int
    ) -> SocialMediaPost:
        """Primary method: fetch via Playwright page navigation + web API."""
        page = await ctx.new_page()

        try:
            nav_url = f"https://www.douyin.com/video/{aweme_id}"
            logger.info(f"douyin: navigating to {nav_url}")
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(5000)

            # Fetch aweme detail via web API (in-page fetch reuses browser session)
            aweme_detail = await self._fetch_aweme_detail(page, aweme_id)

            # Fallback: try RENDER_DATA if API didn't work
            if not aweme_detail:
                aweme_detail = await self._extract_from_render_data(page, aweme_id)

            if not aweme_detail:
                raise RuntimeError(f"Failed to get aweme detail for {aweme_id}")

            desc = aweme_detail.get("desc", "")
            author_info = aweme_detail.get("author", {})
            statistics = aweme_detail.get("statistics", {})

            # Author with extended fields
            avatar_thumb = author_info.get("avatar_thumb", {})
            avatar_urls = avatar_thumb.get("url_list", []) if avatar_thumb else []
            author = Author(
                name=author_info.get("nickname", "unknown"),
                id=author_info.get("sec_uid", "") or str(author_info.get("uid", "")),
                avatar=avatar_urls[0] if avatar_urls else None,
                signature=author_info.get("signature") or None,
                ip_location=author_info.get("ip_location") or None,
            )

            stats = {
                "likes": statistics.get("digg_count", 0),
                "comments": statistics.get("comment_count", 0),
                "shares": statistics.get("share_count", 0),
                "plays": statistics.get("play_count", 0),
                "favorites": statistics.get("collect_count", 0),
            }

            # Music / BGM info
            music_info = aweme_detail.get("music")
            music = None
            if music_info:
                music = MusicInfo(
                    title=music_info.get("title", ""),
                    author=music_info.get("author", ""),
                    duration=music_info.get("duration", 0),
                )

            # Creation time and content type
            create_time = aweme_detail.get("create_time")
            aweme_type_raw = aweme_detail.get("aweme_type")
            aweme_type = None
            if aweme_type_raw is not None:
                type_map = {0: "video", 68: "image", 150: "image"}
                aweme_type = type_map.get(aweme_type_raw, f"type_{aweme_type_raw}")

            # Extract images (for image/note posts)
            images = []
            image_list = aweme_detail.get("images") or []
            for img in image_list:
                url_list = img.get("url_list", [])
                if url_list:
                    images.append(url_list[-1])  # last is usually highest quality

            # Extract video URL
            video_url = None
            video = aweme_detail.get("video", {})
            if video:
                play_addr = video.get("play_addr", {})
                url_list = play_addr.get("url_list", [])
                if url_list:
                    video_url = url_list[0]

                # If no images, use video cover as image
                if not images:
                    cover = video.get("cover", {}) or video.get("origin_cover", {})
                    cover_urls = cover.get("url_list", [])
                    if cover_urls:
                        images.append(cover_urls[-1])

            # Fetch comments
            comments = await self._fetch_comments(page, aweme_id, max_comments)

            return SocialMediaPost(
                platform="douyin",
                title=desc[:80] if desc else "(无标题)",
                author=author,
                content=desc,
                stats=stats,
                images=images,
                video_url=video_url,
                music=music,
                create_time=create_time,
                aweme_type=aweme_type,
                comments=comments,
                url=url,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

        finally:
            await page.close()

    async def _fetch_aweme_detail(self, page, aweme_id: str) -> dict | None:
        """Fetch aweme detail via Douyin web API (in-page fetch)."""
        try:
            result = await page.evaluate(
                """async (awemeId) => {
                    try {
                        const resp = await fetch(
                            `https://www.douyin.com/aweme/v1/web/aweme/detail/?aweme_id=${awemeId}&aid=6383&device_platform=web`,
                            { credentials: 'include' }
                        );
                        const data = await resp.json();
                        return JSON.stringify(data);
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                aweme_id,
            )
            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"douyin: web API error: {data['error']}")
                return None
            detail = data.get("aweme_detail")
            if detail:
                logger.info("douyin: got aweme detail via web API")
            return detail
        except Exception as e:
            logger.warning(f"douyin: web API fetch failed: {e}")
            return None

    async def _extract_from_render_data(self, page, aweme_id: str) -> dict | None:
        """Fallback: extract aweme detail from RENDER_DATA script tag."""
        try:
            render_data = await page.evaluate(
                """() => {
                    try {
                        const el = document.getElementById('RENDER_DATA');
                        if (!el) return null;
                        const decoded = decodeURIComponent(el.textContent);
                        return JSON.parse(decoded);
                    } catch(e) {
                        return null;
                    }
                }"""
            )
            if not render_data:
                return None
            return self._find_aweme_detail(render_data, aweme_id)
        except Exception as e:
            logger.warning(f"douyin: RENDER_DATA extraction failed: {e}")
            return None

    def _find_aweme_detail(self, render_data: dict, aweme_id: str) -> dict | None:
        """Recursively find aweme detail object in RENDER_DATA."""
        for key, value in render_data.items():
            if not isinstance(value, dict):
                continue
            detail = value.get("awemeDetail")
            if detail:
                return detail
            aweme = value.get("aweme", {})
            if isinstance(aweme, dict):
                detail = aweme.get("detail")
                if detail:
                    return detail
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, dict):
                    detail = sub_value.get("awemeDetail")
                    if detail:
                        return detail
        return None

    async def _resolve_short_url(self, ctx, url: str) -> str:
        """Resolve v.douyin.com short URL via Playwright."""
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(3000)
            resolved = page.url
            if DOUYIN_SHORTLINK_PREFIX in resolved:
                try:
                    await page.wait_for_url("**/douyin.com/**", timeout=10000)
                    resolved = page.url
                except Exception:
                    logger.warning(f"douyin: short URL did not redirect to douyin.com: {resolved}")
            return resolved
        finally:
            await page.close()

    async def _fetch_comments(self, page, aweme_id: str, max_comments: int) -> List[Comment]:
        """Fetch comments via in-page fetch to Douyin comment API."""
        if max_comments <= 0:
            return []

        try:
            result = await page.evaluate(
                """async ({awemeId, count}) => {
                    try {
                        const resp = await fetch(
                            `https://www.douyin.com/aweme/v1/web/comment/list/?aweme_id=${awemeId}&cursor=0&count=${count}&item_type=0`,
                            { credentials: 'include' }
                        );
                        const data = await resp.json();
                        return JSON.stringify(data);
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                {"awemeId": aweme_id, "count": max_comments},
            )

            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"douyin: comment fetch error: {data['error']}")
                return []

            comments_data = data.get("comments") or []
            comments = []
            for c in comments_data[:max_comments]:
                c_user = c.get("user", {})
                comments.append(
                    Comment(
                        user=c_user.get("nickname", "anonymous"),
                        text=c.get("text", ""),
                        likes=c.get("digg_count", 0),
                        time=str(c.get("create_time", "")),
                        ip_location=c.get("ip_label") or None,
                        sub_comment_count=c.get("reply_comment_total", 0),
                    )
                )
            return comments

        except Exception as e:
            logger.warning(f"douyin: failed to fetch comments: {e}")
            return []
