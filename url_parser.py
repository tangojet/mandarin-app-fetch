"""URL detection, platform routing, and ID extraction."""

from __future__ import annotations

import re
from typing import Optional

import httpx

# Platform URL patterns
XHS_PATTERNS = [
    # Full explore URL: xiaohongshu.com/explore/{noteId}
    re.compile(r"https?://(?:www\.)?xiaohongshu\.com/explore/([a-zA-Z0-9]+)"),
    # Discovery URL: xiaohongshu.com/discovery/item/{noteId}
    re.compile(r"https?://(?:www\.)?xiaohongshu\.com/discovery/item/([a-zA-Z0-9]+)"),
]
XHS_SHORTLINK = re.compile(r"https?://xhslink\.com/\S+")

# Douyin URL patterns
DOUYIN_PATTERNS = [
    # Video page: douyin.com/video/{awemeId}
    re.compile(r"https?://(?:www\.)?douyin\.com/video/(\d+)"),
    # Note page: douyin.com/note/{awemeId}
    re.compile(r"https?://(?:www\.)?douyin\.com/note/(\d+)"),
    # Share page: iesdouyin.com/share/video/{awemeId} (short URL redirect target)
    re.compile(r"https?://(?:www\.)?iesdouyin\.com/share/video/(\d+)"),
]
DOUYIN_SHORTLINK = re.compile(r"https?://v\.douyin\.com/[a-zA-Z0-9_\-]+/?")

# Bilibili URL patterns
BILIBILI_PATTERNS = [
    # BV video: bilibili.com/video/BVxxxx
    re.compile(r"https?://(?:www\.)?bilibili\.com/video/(BV[a-zA-Z0-9]+)"),
    # AV video: bilibili.com/video/avxxxx
    re.compile(r"https?://(?:www\.)?bilibili\.com/video/av(\d+)", re.IGNORECASE),
]
BILIBILI_SHORTLINK = re.compile(r"https?://b23\.tv/[a-zA-Z0-9]+")

# Xueqiu URL patterns
XUEQIU_PATTERNS = [
    # Post: xueqiu.com/{userId}/{postId} (both numeric)
    re.compile(r"https?://(?:www\.)?xueqiu\.com/(\d+)/(\d+)"),
]

# Toutiao URL patterns
TOUTIAO_PATTERNS = [
    # Article page: toutiao.com/article/{itemId}
    re.compile(r"https?://(?:www\.)?toutiao\.com/article/(\d+)"),
    # Video page: toutiao.com/video/(\d+)
    re.compile(r"https?://(?:www\.)?toutiao\.com/video/(\d+)"),
]

# WeChat article URL patterns
WECHAT_PATTERNS = [
    # mp.weixin.qq.com/s/{id} or mp.weixin.qq.com/s?__biz=...
    re.compile(r"https?://mp\.weixin\.qq\.com/s/[A-Za-z0-9_-]+"),
    re.compile(r"https?://mp\.weixin\.qq\.com/s\?[^\s]+"),
]

# Goofish (闲鱼) URL patterns
GOOFISH_PATTERNS = [
    # Item page: goofish.com/item?id={itemId} (id can be any query param position)
    re.compile(r"https?://(?:www\.)?goofish\.com/item\?(?:[^#]*&)?id=(\d+)"),
    # Mobile: h5.m.goofish.com/item?id={itemId}
    re.compile(r"https?://h5\.m\.goofish\.com/item\?(?:[^#]*&)?id=(\d+)"),
    # Legacy idle.taobao URLs
    re.compile(r"https?://market\.m\.taobao\.com/app/idleFish-F2e/.*[?&]id=(\d+)"),
]

# Weibo URL patterns
WEIBO_PATTERNS = [
    # Desktop: weibo.com/{uid}/{id}
    re.compile(r"https?://(?:www\.)?weibo\.com/\d+/([a-zA-Z0-9]+)"),
    # Mobile detail: m.weibo.cn/detail/{id}
    re.compile(r"https?://m\.weibo\.cn/detail/(\d+)"),
    # Mobile status: m.weibo.cn/status/{id}
    re.compile(r"https?://m\.weibo\.cn/status/(\d+)"),
]


def detect_platform(url: str) -> Optional[str]:
    """Return platform name if URL is recognized, else None."""
    for pat in XHS_PATTERNS:
        if pat.search(url):
            return "xhs"
    if XHS_SHORTLINK.search(url):
        return "xhs"

    for pat in DOUYIN_PATTERNS:
        if pat.search(url):
            return "douyin"
    if DOUYIN_SHORTLINK.search(url):
        return "douyin"

    for pat in BILIBILI_PATTERNS:
        if pat.search(url):
            return "bilibili"
    if BILIBILI_SHORTLINK.search(url):
        return "bilibili"

    for pat in GOOFISH_PATTERNS:
        if pat.search(url):
            return "goofish"

    for pat in WECHAT_PATTERNS:
        if pat.search(url):
            return "wechat"

    for pat in WEIBO_PATTERNS:
        if pat.search(url):
            return "weibo"

    for pat in XUEQIU_PATTERNS:
        if pat.search(url):
            return "xueqiu"

    for pat in TOUTIAO_PATTERNS:
        if pat.search(url):
            return "toutiao"

    return None


def extract_xhs_note_id(url: str) -> Optional[str]:
    """Extract note ID from a xiaohongshu.com URL."""
    for pat in XHS_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def extract_douyin_aweme_id(url: str) -> Optional[str]:
    """Extract aweme ID from a douyin.com URL."""
    for pat in DOUYIN_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def extract_bilibili_bvid(url: str) -> Optional[str]:
    """Extract BV or AV id from a bilibili.com URL."""
    for pat in BILIBILI_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def extract_weibo_id(url: str) -> Optional[str]:
    """Extract weibo post ID from a weibo URL."""
    for pat in WEIBO_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def extract_xueqiu_post_id(url: str) -> Optional[str]:
    """Extract post ID from a xueqiu.com URL (second numeric segment)."""
    for pat in XUEQIU_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(2)
    return None


def extract_toutiao_item_id(url: str) -> Optional[str]:
    """Extract item ID from a toutiao.com URL."""
    for pat in TOUTIAO_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


def extract_goofish_item_id(url: str) -> Optional[str]:
    """Extract item ID from a goofish.com URL."""
    for pat in GOOFISH_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None


async def resolve_short_url(url: str) -> str:
    """Resolve short URLs to full URLs via redirect following."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=10) as client:
        resp = await client.get(url)
        return str(resp.url)
