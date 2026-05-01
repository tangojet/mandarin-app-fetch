"""Xueqiu (雪球) fetcher using Playwright page navigation + in-page API calls."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import List, Optional

from browser_manager import get_xueqiu_context
from models import Author, Comment, SocialMediaPost
from platforms.base import BasePlatform
from url_parser import extract_xueqiu_post_id

logger = logging.getLogger("media-fetch-api")


def _strip_html(html: str) -> str:
    """Strip HTML tags, preserving text."""
    if not html:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return text.strip()


class XueqiuPlatform(BasePlatform):
    @property
    def name(self) -> str:
        return "xueqiu"

    async def fetch(self, url: str, max_comments: int = 10) -> SocialMediaPost:
        ctx = await get_xueqiu_context()

        post_id = extract_xueqiu_post_id(url)
        if not post_id:
            raise ValueError(f"Could not extract post ID from URL: {url}")

        page = await ctx.new_page()

        try:
            # Navigate to xueqiu.com first to establish same-origin context
            nav_url = f"https://xueqiu.com"
            logger.info(f"xueqiu: navigating to {nav_url}")
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Fetch post data via in-page fetch (bypasses WAF since it's same-origin)
            post_data = await page.evaluate(
                """async (postId) => {
                    try {
                        const resp = await fetch(
                            `/statuses/show.json?id=${postId}`,
                            { credentials: 'include' }
                        );
                        const data = await resp.text();
                        return data;
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                post_id,
            )

            status = json.loads(post_data)
            if status.get("error"):
                raise RuntimeError(f"Xueqiu API error: {status['error']}")

            if not status or (not status.get("text") and not status.get("description") and not status.get("title")):
                raise RuntimeError(f"No post data returned for xueqiu post {post_id}")

            # Extract fields
            raw_text = status.get("text", "") or status.get("description", "")
            content = _strip_html(raw_text)
            user = status.get("user", {}) or {}

            author = Author(
                name=user.get("screen_name", "unknown"),
                id=str(user.get("id", "")),
            )

            stats = {
                "likes": status.get("fav_count", 0) or status.get("like_count", 0),
                "comments": status.get("reply_count", 0),
                "reposts": status.get("retweet_count", 0),
            }

            # Xueqiu posts don't have standalone images/videos in the API response
            images: List[str] = []
            video_url: Optional[str] = None

            # Fetch comments
            comments = await self._fetch_comments(page, post_id, max_comments)

            # Title
            title = status.get("title") or ""
            if not title:
                title = content.split("\n")[0][:80] if content else "(无标题)"

            return SocialMediaPost(
                platform="xueqiu",
                title=title,
                author=author,
                content=content,
                stats=stats,
                images=images,
                video_url=video_url,
                comments=comments,
                url=url,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

        finally:
            await page.close()

    async def _fetch_comments(self, page, post_id: str, max_comments: int) -> List[Comment]:
        """Fetch comments via in-page fetch to xueqiu API."""
        if max_comments <= 0:
            return []

        try:
            result = await page.evaluate(
                """async ({postId, maxComments}) => {
                    try {
                        const resp = await fetch(
                            `/statuses/comments.json?id=${postId}&count=${maxComments}&page=1`,
                            { credentials: 'include' }
                        );
                        const data = await resp.text();
                        return data;
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                {"postId": post_id, "maxComments": max_comments},
            )

            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"xueqiu: comment fetch error: {data['error']}")
                return []

            comments_data = data.get("comments") or []

            comments = []
            for c in comments_data[:max_comments]:
                c_user = c.get("user", {}) or {}
                c_text = _strip_html(c.get("text", ""))
                comments.append(
                    Comment(
                        user=c_user.get("screen_name", "anonymous"),
                        text=c_text,
                        likes=c.get("like_count", 0),
                        time=str(c.get("created_at", "")),
                    )
                )
            return comments

        except Exception as e:
            logger.warning(f"xueqiu: failed to fetch comments: {e}")
            return []

    async def search(self, query: str, count: int = 5) -> list[dict]:
        """Search Xueqiu posts by keyword. Returns list of result dicts."""
        ctx = await get_xueqiu_context()
        page = await ctx.new_page()

        try:
            # Navigate to xueqiu.com to establish same-origin context
            await page.goto("https://xueqiu.com", wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            result = await page.evaluate(
                """async ({query, count}) => {
                    try {
                        const q = encodeURIComponent(query);
                        const resp = await fetch(
                            `/query/v1/search/status.json?q=${q}&count=${count}&page=1`,
                            { credentials: 'include' }
                        );
                        const data = await resp.text();
                        return data;
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                {"query": query, "count": count},
            )

            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"xueqiu: search error: {data['error']}")
                return []

            items = data.get("list") or []
            results = []
            for item in items[:count]:
                user = item.get("user", {}) or {}
                desc = _strip_html(item.get("description", ""))[:200] if item.get("description") else ""
                results.append({
                    "id": item.get("id"),
                    "title": item.get("title") or "(无标题)",
                    "author": user.get("screen_name", "unknown"),
                    "author_id": str(user.get("id", "")),
                    "description": desc,
                    "url": f"https://xueqiu.com{item['target']}" if item.get("target") else "",
                    "created_at": item.get("created_at"),
                    "stats": {
                        "likes": item.get("fav_count", 0) or item.get("like_count", 0),
                        "comments": item.get("reply_count", 0),
                        "reposts": item.get("retweet_count", 0),
                    },
                })

            return results

        finally:
            await page.close()
