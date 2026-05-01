"""Weibo (微博) fetcher using Playwright page navigation + mobile API."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import List

from browser_manager import get_weibo_context
from models import Author, Comment, SocialMediaPost
from platforms.base import BasePlatform
from url_parser import extract_weibo_id

logger = logging.getLogger("media-fetch-api")


def _strip_html(html: str) -> str:
    """Strip HTML tags from weibo content, preserving text."""
    if not html:
        return ""
    # Replace <br> and <br/> with newlines
    text = re.sub(r"<br\s*/?>", "\n", html)
    # Remove all remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode common HTML entities
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " ")
    return text.strip()


class WeiboPlatform(BasePlatform):
    @property
    def name(self) -> str:
        return "weibo"

    async def fetch(self, url: str, max_comments: int = 10) -> SocialMediaPost:
        ctx = await get_weibo_context()

        weibo_id = extract_weibo_id(url)
        if not weibo_id:
            raise ValueError(f"Could not extract weibo ID from URL: {url}")

        page = await ctx.new_page()

        try:
            # Navigate to mobile weibo detail page
            nav_url = f"https://m.weibo.cn/detail/{weibo_id}"
            logger.info(f"weibo: navigating to {nav_url}")
            await page.goto(nav_url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Fetch post data via mobile API (in-page fetch to reuse cookies)
            post_data = await page.evaluate(
                """async (weiboId) => {
                    try {
                        const resp = await fetch(
                            `https://m.weibo.cn/statuses/show?id=${weiboId}`,
                            { credentials: 'include' }
                        );
                        const data = await resp.json();
                        return JSON.stringify(data);
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                weibo_id,
            )

            data = json.loads(post_data)
            if data.get("error"):
                raise RuntimeError(f"Weibo API error: {data['error']}")

            status = data.get("data", {})
            if not status:
                raise RuntimeError(f"No status data returned for weibo {weibo_id}")

            # Extract fields
            raw_text = status.get("text", "")
            content = _strip_html(raw_text)
            user = status.get("user", {})

            author = Author(
                name=user.get("screen_name", "unknown"),
                id=str(user.get("id", "")),
            )

            stats = {
                "likes": status.get("attitudes_count", 0),
                "comments": status.get("comments_count", 0),
                "reposts": status.get("reposts_count", 0),
            }

            # Extract images
            images = []
            pics = status.get("pics", [])
            for pic in pics:
                large = pic.get("large", {})
                img_url = large.get("url") or pic.get("url", "")
                if img_url:
                    images.append(img_url)

            # Extract video
            video_url = None
            page_info = status.get("page_info", {})
            if page_info.get("type") == "video":
                media_info = page_info.get("media_info", {}) or page_info.get("urls", {})
                # Try various quality keys
                for key in ["mp4_720p_mp4", "mp4_hd_url", "mp4_sd_url", "stream_url"]:
                    v = media_info.get(key)
                    if v:
                        video_url = v
                        break

            # Fetch comments
            comments = await self._fetch_comments(page, weibo_id, max_comments)

            # Use first line of content as title if no explicit title
            title_text = content.split("\n")[0][:80] if content else "(无标题)"

            return SocialMediaPost(
                platform="weibo",
                title=title_text,
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

    async def _fetch_comments(self, page, weibo_id: str, max_comments: int) -> List[Comment]:
        """Fetch comments via in-page fetch to mobile weibo API."""
        if max_comments <= 0:
            return []

        try:
            result = await page.evaluate(
                """async ({weiboId, maxComments}) => {
                    try {
                        const resp = await fetch(
                            `https://m.weibo.cn/api/comments/show?id=${weiboId}&page=1`,
                            { credentials: 'include' }
                        );
                        const data = await resp.json();
                        return JSON.stringify(data);
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                {"weiboId": weibo_id, "maxComments": max_comments},
            )

            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"weibo: comment fetch error: {data['error']}")
                return []

            hot_comments = data.get("data", {}).get("hot_data") or []
            all_comments = data.get("data", {}).get("data") or []
            # Prefer hot comments, fallback to regular
            comments_data = hot_comments or all_comments

            comments = []
            for c in comments_data[:max_comments]:
                c_user = c.get("user", {})
                c_text = _strip_html(c.get("text", ""))
                comments.append(
                    Comment(
                        user=c_user.get("screen_name", "anonymous"),
                        text=c_text,
                        likes=c.get("like_count", 0),
                        time=c.get("created_at", ""),
                    )
                )
            return comments

        except Exception as e:
            logger.warning(f"weibo: failed to fetch comments: {e}")
            return []
