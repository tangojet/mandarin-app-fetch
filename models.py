"""Pydantic response models for the unified social media fetch API."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel


class Author(BaseModel):
    name: str
    id: str = ""
    avatar: Optional[str] = None
    signature: Optional[str] = None
    ip_location: Optional[str] = None


class MusicInfo(BaseModel):
    title: str = ""
    author: str = ""
    duration: int = 0


class Comment(BaseModel):
    user: str
    text: str
    likes: int = 0
    time: str = ""
    ip_location: Optional[str] = None
    sub_comment_count: int = 0


class SocialMediaPost(BaseModel):
    platform: str
    title: str
    author: Author
    content: str
    stats: Dict[str, int]
    images: List[str] = []
    video_url: Optional[str] = None
    music: Optional[MusicInfo] = None
    create_time: Optional[int] = None
    aweme_type: Optional[str] = None
    comments: List[Comment] = []
    url: str
    fetched_at: str
