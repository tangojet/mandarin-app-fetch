"""Xiaohongshu (小红书) fetcher — uses Docker browser CDP when available, Playwright fallback."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import unquote, urlparse, parse_qs

from browser_manager import get_xhs_context
from models import Author, Comment, SocialMediaPost
from platforms.base import BasePlatform
from url_parser import extract_xhs_note_id

logger = logging.getLogger("media-fetch-api")

XHS_SHORTLINK_PREFIX = "xhslink.com"

# Docker container name with a logged-in Chrome browser for XHS
XHS_DOCKER_CONTAINER = os.environ.get("XHS_DOCKER_CONTAINER", "test-two-browser")
XHS_CDP_SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "xhs-cdp-extract.js")


def _find_docker() -> Optional[str]:
    """Find the docker binary path."""
    docker = shutil.which("docker")
    if docker:
        return docker
    # macOS Docker Desktop common paths
    for p in ["/Applications/Docker.app/Contents/Resources/bin/docker", "/usr/local/bin/docker"]:
        if os.path.isfile(p):
            return p
    return None


class XhsPlatform(BasePlatform):
    @property
    def name(self) -> str:
        return "xhs"

    def __init__(self):
        self._docker_bin = _find_docker()
        self._cdp_script_copied = False

    async def _ensure_cdp_script(self):
        """Copy the CDP extract script into the Docker container."""
        if self._cdp_script_copied or not self._docker_bin:
            return
        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker_bin, "cp", XHS_CDP_SCRIPT,
                f"{XHS_DOCKER_CONTAINER}:/tmp/xhs-cdp-extract.js",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.wait()
            if proc.returncode == 0:
                self._cdp_script_copied = True
                logger.info("xhs: CDP extract script copied to Docker container")
        except Exception as e:
            logger.warning(f"xhs: failed to copy CDP script: {e}")

    async def _fetch_via_docker(self, url: str, max_comments: int) -> Optional[SocialMediaPost]:
        """Try to fetch using the Docker browser's CDP. Returns None if unavailable."""
        if not self._docker_bin:
            return None

        await self._ensure_cdp_script()
        if not self._cdp_script_copied:
            return None

        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker_bin, "exec", XHS_DOCKER_CONTAINER,
                "node", "/tmp/xhs-cdp-extract.js", url, str(max_comments),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=35)

            if stderr:
                logger.info(f"xhs-cdp: {stderr.decode().strip()}")

            if proc.returncode != 0:
                logger.warning(f"xhs-cdp: exited {proc.returncode}")
                return None

            data = json.loads(stdout.decode())
            if data.get("error"):
                logger.warning(f"xhs-cdp: {data['error']}")
                return None

            return SocialMediaPost(
                platform=data["platform"],
                title=data["title"],
                author=Author(name=data["author"]["name"], id=data["author"]["id"]),
                content=data["content"],
                stats=data["stats"],
                images=data.get("images", []),
                video_url=data.get("video_url"),
                comments=[Comment(**c) for c in data.get("comments", [])],
                url=data["url"],
                fetched_at=data["fetched_at"],
            )
        except asyncio.TimeoutError:
            logger.warning("xhs-cdp: timed out")
            return None
        except Exception as e:
            logger.warning(f"xhs-cdp: {e}")
            return None

    async def fetch(self, url: str, max_comments: int = 10) -> SocialMediaPost:
        ctx = await get_xhs_context()

        # Resolve short links using Playwright (xhslink.com uses JS redirects,
        # not HTTP 301/302, so httpx follow_redirects doesn't work)
        if XHS_SHORTLINK_PREFIX in url:
            url = await self._resolve_short_url(ctx, url)
            logger.info(f"xhs: resolved short URL to {url}")

        note_id = extract_xhs_note_id(url)
        if not note_id:
            raise ValueError(f"Could not extract note ID from URL: {url}")

        # Preserve original query params (xsec_token, xsec_source, etc.)
        parsed = urlparse(url)
        if parsed.query:
            nav_url = f"https://www.xiaohongshu.com/explore/{note_id}?{parsed.query}"
        else:
            nav_url = f"https://www.xiaohongshu.com/explore/{note_id}"

        # Try Docker browser first (bypasses anti-bot fingerprinting)
        docker_result = await self._fetch_via_docker(nav_url, max_comments)
        if docker_result:
            docker_result.url = url  # preserve original URL
            return docker_result

        # Fallback: use Playwright (works when cookies + stealth are sufficient)
        logger.info("xhs: Docker browser unavailable, falling back to Playwright")
        page = await ctx.new_page()

        try:
            logger.info(f"xhs: navigating to {nav_url}")
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            initial_state = await page.evaluate(
                """() => {
                    try {
                        return JSON.parse(JSON.stringify(window.__INITIAL_STATE__));
                    } catch(e) {
                        return null;
                    }
                }"""
            )

            if not initial_state:
                raise RuntimeError("Failed to extract __INITIAL_STATE__ from page")

            note_detail_map = initial_state.get("note", {}).get("noteDetailMap", {})
            note_data = None
            for key, val in note_detail_map.items():
                note_data = val.get("note")
                if note_data:
                    break

            if not note_data:
                raise RuntimeError(f"No note data found in __INITIAL_STATE__ for {note_id}")

            title = note_data.get("title", "")
            desc = note_data.get("desc", "")
            user_info = note_data.get("user", {})
            interact_info = note_data.get("interactInfo", {})
            image_list = note_data.get("imageList", [])

            author = Author(
                name=user_info.get("nickname", "unknown"),
                id=user_info.get("userId", ""),
            )

            stats = {
                "likes": int(interact_info.get("likedCount", "0")),
                "comments": int(interact_info.get("commentCount", "0")),
                "shares": int(interact_info.get("shareCount", "0")),
                "collects": int(interact_info.get("collectedCount", "0")),
            }

            images = []
            for img in image_list:
                info_list = img.get("infoList", [])
                if info_list:
                    best = max(info_list, key=lambda x: x.get("width", 0) * x.get("height", 0))
                    img_url = best.get("url", "")
                    if img_url and not img_url.startswith("http"):
                        img_url = "https:" + img_url
                    if img_url:
                        images.append(img_url)
                elif img.get("urlDefault"):
                    img_url = img["urlDefault"]
                    if not img_url.startswith("http"):
                        img_url = "https:" + img_url
                    images.append(img_url)

            video_url = None
            video = note_data.get("video", {})
            if video:
                media = video.get("media", {})
                stream = media.get("stream", {})
                for quality in ["h264", "h265", "av1"]:
                    streams = stream.get(quality, [])
                    if streams:
                        video_url = streams[0].get("masterUrl") or streams[0].get("backupUrls", [""])[0]
                        break

            comments = await self._fetch_comments(page, note_id, max_comments)

            return SocialMediaPost(
                platform="xhs",
                title=title or "(无标题)",
                author=author,
                content=desc,
                stats=stats,
                images=images,
                video_url=video_url,
                comments=comments,
                url=url,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

        finally:
            await page.close()

    async def _resolve_short_url(self, ctx, url: str) -> str:
        """Resolve xhslink.com short URL via Playwright (handles JS redirects)."""
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
            resolved = page.url
            if XHS_SHORTLINK_PREFIX in resolved:
                try:
                    await page.wait_for_url("**/xiaohongshu.com/**", timeout=10000)
                    resolved = page.url
                except Exception:
                    logger.warning(f"xhs: short URL did not redirect to xiaohongshu.com: {resolved}")

            # Handle login redirect: extract actual URL from redirectPath param
            parsed = urlparse(resolved)
            if parsed.path == "/login" and "redirectPath" in parsed.query:
                redirect_path = parse_qs(parsed.query).get("redirectPath", [""])[0]
                if redirect_path:
                    resolved = unquote(redirect_path)
                    logger.info(f"xhs: extracted URL from login redirect: {resolved}")

            return resolved
        finally:
            await page.close()

    async def _fetch_comments(self, page, note_id: str, max_comments: int) -> List[Comment]:
        """Fetch comments using in-page fetch to reuse cookies and signatures."""
        try:
            result = await page.evaluate(
                """async ({noteId, limit}) => {
                    try {
                        const resp = await fetch(
                            `/api/sns/web/v2/comment/page?note_id=${noteId}&cursor=&top_comment_id=&image_formats=jpg,webp,avif&num=${limit}`,
                            { credentials: 'include' }
                        );
                        const data = await resp.json();
                        return JSON.stringify(data);
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                {"noteId": note_id, "limit": max_comments},
            )

            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"xhs: comment fetch error: {data['error']}")
                return []

            comments_data = data.get("data", {}).get("comments", [])
            comments = []
            for c in comments_data[:max_comments]:
                user_info = c.get("user_info", {})
                comments.append(
                    Comment(
                        user=user_info.get("nickname", "anonymous"),
                        text=c.get("content", ""),
                        likes=int(c.get("like_count", "0")),
                        time=c.get("create_time", ""),
                    )
                )
            return comments

        except Exception as e:
            logger.warning(f"xhs: failed to fetch comments: {e}")
            return []
