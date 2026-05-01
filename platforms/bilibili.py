"""Bilibili (B站) fetcher using Playwright page navigation + __INITIAL_STATE__ parsing."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List

from browser_manager import get_bilibili_context
from models import Author, Comment, SocialMediaPost
from platforms.base import BasePlatform
from url_parser import extract_bilibili_bvid

logger = logging.getLogger("media-fetch-api")

BILIBILI_SHORTLINK_PREFIX = "b23.tv"


class BilibiliPlatform(BasePlatform):
    @property
    def name(self) -> str:
        return "bilibili"

    async def fetch(self, url: str, max_comments: int = 10) -> SocialMediaPost:
        ctx = await get_bilibili_context()

        # Resolve short links
        if BILIBILI_SHORTLINK_PREFIX in url:
            url = await self._resolve_short_url(ctx, url)
            logger.info(f"bilibili: resolved short URL to {url}")

        bvid = extract_bilibili_bvid(url)
        if not bvid:
            raise ValueError(f"Could not extract BV/AV id from URL: {url}")

        # Normalize: if it's an AV id (all digits), prefix with "av"
        is_avid = bvid.isdigit()
        video_path = f"av{bvid}" if is_avid else bvid

        page = await ctx.new_page()

        try:
            nav_url = f"https://www.bilibili.com/video/{video_path}"
            logger.info(f"bilibili: navigating to {nav_url}")
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Extract __INITIAL_STATE__ from the page
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
                raise RuntimeError("Failed to extract __INITIAL_STATE__ from Bilibili page")

            video_data = initial_state.get("videoData")
            if not video_data:
                raise RuntimeError(f"No videoData found in __INITIAL_STATE__ for {video_path}")

            title = video_data.get("title", "")
            desc = video_data.get("desc", "")
            owner = video_data.get("owner", {})
            stat = video_data.get("stat", {})
            aid = video_data.get("aid", 0)

            author = Author(
                name=owner.get("name", "unknown"),
                id=str(owner.get("mid", "")),
            )

            stats = {
                "views": stat.get("view", 0),
                "likes": stat.get("like", 0),
                "coins": stat.get("coin", 0),
                "favorites": stat.get("favorite", 0),
                "shares": stat.get("share", 0),
                "danmaku": stat.get("danmaku", 0),
                "comments": stat.get("reply", 0),
            }

            # Cover image
            cover = video_data.get("pic", "")
            if cover and not cover.startswith("http"):
                cover = "https:" + cover
            images = [cover] if cover else []

            # Bilibili uses DASH encrypted streams, no direct video URL
            video_url = None

            # Fetch comments
            comments = await self._fetch_comments(page, aid, max_comments)

            return SocialMediaPost(
                platform="bilibili",
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
        """Resolve b23.tv short URL via Playwright."""
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            await page.wait_for_timeout(2000)
            resolved = page.url
            if BILIBILI_SHORTLINK_PREFIX in resolved:
                try:
                    await page.wait_for_url("**/bilibili.com/**", timeout=10000)
                    resolved = page.url
                except Exception:
                    logger.warning(f"bilibili: short URL did not redirect to bilibili.com: {resolved}")
            return resolved
        finally:
            await page.close()

    async def _fetch_comments(self, page, aid: int, max_comments: int) -> List[Comment]:
        """Fetch comments via in-page fetch to api.bilibili.com."""
        if not aid or max_comments <= 0:
            return []

        try:
            result = await page.evaluate(
                """async ({aid, pageSize}) => {
                    try {
                        const resp = await fetch(
                            `https://api.bilibili.com/x/v2/reply/main?type=1&oid=${aid}&mode=3&ps=${pageSize}`,
                            { credentials: 'include' }
                        );
                        const data = await resp.json();
                        return JSON.stringify(data);
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                {"aid": aid, "pageSize": max_comments},
            )

            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"bilibili: comment fetch error: {data['error']}")
                return []

            replies = data.get("data", {}).get("replies") or []
            comments = []
            for r in replies[:max_comments]:
                member = r.get("member", {})
                content = r.get("content", {})
                comments.append(
                    Comment(
                        user=member.get("uname", "anonymous"),
                        text=content.get("message", ""),
                        likes=r.get("like", 0),
                        time=str(r.get("ctime", "")),
                    )
                )
            return comments

        except Exception as e:
            logger.warning(f"bilibili: failed to fetch comments: {e}")
            return []
