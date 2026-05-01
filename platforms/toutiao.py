"""Toutiao (今日头条) fetcher using Playwright page navigation + web API."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import List

from browser_manager import get_toutiao_context
from models import Author, Comment, SocialMediaPost
from platforms.base import BasePlatform
from url_parser import extract_toutiao_item_id

logger = logging.getLogger("media-fetch-api")


class ToutiaoPlatform(BasePlatform):
    @property
    def name(self) -> str:
        return "toutiao"

    async def fetch(self, url: str, max_comments: int = 10) -> SocialMediaPost:
        ctx = await get_toutiao_context()

        item_id = extract_toutiao_item_id(url)
        if not item_id:
            raise ValueError(f"Could not extract item_id from URL: {url}")

        page = await ctx.new_page()

        try:
            logger.info(f"toutiao: navigating to {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(3000)

            # Try API-based extraction first
            detail = await self._fetch_article_detail(page, item_id)

            # Fallback: extract from page DOM / SSR state
            if not detail:
                detail = await self._extract_from_page(page)

            if not detail:
                raise RuntimeError(f"Failed to extract content for {item_id}")

            title = detail.get("title", "")
            content = detail.get("content", "")
            author_info = detail.get("author", {})

            author = Author(
                name=author_info.get("name", "unknown"),
                id=str(author_info.get("id", "")),
                avatar=author_info.get("avatar") or None,
            )

            stats = detail.get("stats", {})
            images = detail.get("images", [])
            video_url = detail.get("video_url")
            create_time = detail.get("create_time")

            # Fetch comments
            comments = await self._fetch_comments(page, item_id, max_comments)

            return SocialMediaPost(
                platform="toutiao",
                title=title[:80] if title else "(无标题)",
                author=author,
                content=content,
                stats=stats,
                images=images,
                video_url=video_url,
                create_time=create_time,
                comments=comments,
                url=url,
                fetched_at=datetime.now(timezone.utc).isoformat(),
            )

        finally:
            await page.close()

    async def _fetch_article_detail(self, page, item_id: str) -> dict | None:
        """Fetch article/video detail via Toutiao web API (in-page fetch)."""
        try:
            result = await page.evaluate(
                """async (itemId) => {
                    try {
                        const resp = await fetch(
                            `/api/pc/article/detail/?article_id=${itemId}&source=detail`,
                            { credentials: 'include' }
                        );
                        const data = await resp.json();
                        return JSON.stringify(data);
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                item_id,
            )
            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"toutiao: API error: {data['error']}")
                return None

            # Parse the API response — Toutiao nests data under data.data or data
            inner = data.get("data", {})
            article_info = inner.get("article_info", inner)

            if not article_info.get("title"):
                logger.warning("toutiao: no title in API response")
                return None

            logger.info("toutiao: got detail via web API")

            # Author
            author_info = inner.get("author_info", {})

            # Content — strip HTML tags for plain text
            raw_content = article_info.get("content", "") or article_info.get("abstract", "")

            # Extract images from HTML content
            images = []
            for img_match in re.findall(r'<img[^>]+src="([^"]+)"', raw_content):
                if img_match.startswith("http"):
                    images.append(img_match)

            # Also check image_list field
            for img in article_info.get("image_list", []):
                img_url = img.get("url", "")
                if img_url:
                    if not img_url.startswith("http"):
                        img_url = "https:" + img_url
                    images.append(img_url)

            clean_content = re.sub(r"<[^>]+>", "", raw_content).strip()

            # Video URL
            video_url = None
            video_info = article_info.get("video_info", {}) or article_info.get("video", {})
            if video_info:
                video_url = (
                    video_info.get("video_url")
                    or video_info.get("main_url")
                    or video_info.get("origin_video_url")
                )

            # Stats
            interact_info = inner.get("interact_info", {})
            stats = {
                "likes": interact_info.get("digg_count", 0) or article_info.get("digg_count", 0),
                "comments": interact_info.get("comment_count", 0) or article_info.get("comment_count", 0),
                "shares": interact_info.get("share_count", 0) or article_info.get("share_count", 0),
                "collects": interact_info.get("collect_count", 0) or article_info.get("collect_count", 0),
                "views": article_info.get("read_count", 0),
            }

            create_time = article_info.get("publish_time") or article_info.get("create_time")

            return {
                "title": article_info.get("title", ""),
                "content": clean_content or article_info.get("abstract", ""),
                "author": {
                    "name": author_info.get("name", "") or author_info.get("screen_name", ""),
                    "id": str(author_info.get("user_id", "") or author_info.get("id", "")),
                    "avatar": author_info.get("avatar_url") or author_info.get("avatar"),
                },
                "stats": stats,
                "images": images,
                "video_url": video_url,
                "create_time": create_time,
            }

        except Exception as e:
            logger.warning(f"toutiao: API fetch failed: {e}")
            return None

    async def _extract_from_page(self, page) -> dict | None:
        """Fallback: extract content from rendered page DOM or SSR state."""
        try:
            result = await page.evaluate(
                """() => {
                    try {
                        // Try SSR data
                        if (window.__INITIAL_STATE__) {
                            var state = window.__INITIAL_STATE__;
                            var article = state.articleDetail || state.article || {};
                            if (article.title) {
                                return JSON.stringify({source: "ssr", data: article});
                            }
                        }

                        // Try RENDER_DATA (ByteDance pattern)
                        var renderEl = document.getElementById('RENDER_DATA');
                        if (renderEl) {
                            try {
                                var decoded = decodeURIComponent(renderEl.textContent);
                                var renderData = JSON.parse(decoded);
                                return JSON.stringify({source: "render_data", data: renderData});
                            } catch(e) {}
                        }

                        // Fallback: extract from DOM
                        var title = '';
                        var titleEl = document.querySelector('h1')
                            || document.querySelector('.article-title')
                            || document.querySelector('[class*="title"]');
                        if (titleEl) title = titleEl.textContent.trim();

                        var content = '';
                        var contentEl = document.querySelector('.article-content')
                            || document.querySelector('[class*="articleContent"]')
                            || document.querySelector('[class*="article-body"]')
                            || document.querySelector('article');
                        if (contentEl) content = contentEl.innerText.trim();

                        var authorName = '';
                        var authorEl = document.querySelector('.author-name')
                            || document.querySelector('[class*="authorName"]')
                            || document.querySelector('[class*="author-info"] .name');
                        if (authorEl) authorName = authorEl.textContent.trim();

                        if (!title && !content) return null;

                        return JSON.stringify({
                            source: "dom",
                            data: { title: title, content: content, author: { name: authorName } }
                        });
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }"""
            )

            if not result:
                return None

            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"toutiao: page extraction error: {data['error']}")
                return None

            source = data.get("source", "")
            content_data = data.get("data", {})
            logger.info(f"toutiao: extracted data via {source}")

            if source == "dom":
                return {
                    "title": content_data.get("title", ""),
                    "content": content_data.get("content", ""),
                    "author": content_data.get("author", {}),
                    "stats": {},
                    "images": [],
                    "video_url": None,
                }

            if source == "render_data":
                # RENDER_DATA may contain nested structures — search for article data
                for key, value in content_data.items():
                    if not isinstance(value, dict):
                        continue
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, dict) and sub_value.get("title"):
                            content_data = sub_value
                            break

            # SSR or render_data: try common field names
            title = (
                content_data.get("title", "")
                or content_data.get("articleTitle", "")
            )
            abstract = (
                content_data.get("abstract", "")
                or content_data.get("content", "")
                or content_data.get("desc", "")
            )
            # Strip HTML from abstract
            if "<" in abstract:
                abstract = re.sub(r"<[^>]+>", "", abstract).strip()

            author_info = content_data.get("author", {}) or content_data.get("user", {})

            return {
                "title": title,
                "content": abstract,
                "author": {
                    "name": author_info.get("name", "") or author_info.get("nickname", ""),
                    "id": str(author_info.get("user_id", "") or author_info.get("id", "")),
                    "avatar": author_info.get("avatar_url"),
                },
                "stats": {},
                "images": [],
                "video_url": None,
            }

        except Exception as e:
            logger.warning(f"toutiao: page extraction failed: {e}")
            return None

    async def _fetch_comments(self, page, item_id: str, max_comments: int) -> List[Comment]:
        """Fetch comments via Toutiao comment API."""
        if max_comments <= 0:
            return []

        try:
            result = await page.evaluate(
                """async ({itemId, count}) => {
                    try {
                        const resp = await fetch(
                            `/api/comment/list/?group_id=${itemId}&offset=0&count=${count}`,
                            { credentials: 'include' }
                        );
                        const data = await resp.json();
                        return JSON.stringify(data);
                    } catch(e) {
                        return JSON.stringify({error: e.message});
                    }
                }""",
                {"itemId": item_id, "count": max_comments},
            )

            data = json.loads(result)
            if data.get("error"):
                logger.warning(f"toutiao: comment fetch error: {data['error']}")
                return []

            comments_data = data.get("data", {}).get("comments", []) or data.get("comments", [])
            comments = []
            for c in comments_data[:max_comments]:
                user_info = c.get("user", {})
                comments.append(
                    Comment(
                        user=user_info.get("name", "") or user_info.get("screen_name", "anonymous"),
                        text=c.get("text", "") or c.get("content", ""),
                        likes=c.get("digg_count", 0),
                        time=str(c.get("create_time", "")),
                    )
                )
            return comments

        except Exception as e:
            logger.warning(f"toutiao: failed to fetch comments: {e}")
            return []
