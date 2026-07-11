"""Common adapter interface for all social platforms.

All adapter methods are synchronous; tools_social.py decides which thread
runs them (the pinned browser thread for Instagram, a plain worker thread
for API-based platforms).

Normalized post dict returned by search/get_feed:
    {"platform", "author", "text", "url", "timestamp", "media_urls": [...],
     "engagement": {"likes", "comments", "shares", "views"}}
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class SocialUnsupported(Exception):
    """The platform/tier can't do this — message is shown to the model/owner."""


class LoginChallengeError(Exception):
    """Platform is challenging the session; owner must re-login manually."""

    def __init__(self, message: str, screenshot=None):
        super().__init__(message)
        self.screenshot = screenshot


def make_post(platform: str, author: str = "", text: str = "", url: str = "",
              timestamp: str = "", media_urls: list[str] | None = None,
              likes: int | None = None, comments: int | None = None,
              shares: int | None = None, views: int | None = None) -> dict:
    return {
        "platform": platform,
        "author": author,
        "text": text,
        "url": url,
        "timestamp": timestamp,
        "media_urls": media_urls or [],
        "engagement": {"likes": likes, "comments": comments,
                       "shares": shares, "views": views},
    }


class SocialAdapter(ABC):
    platform: ClassVar[str]
    executor: ClassVar[str] = "default"  # "browser" = pinned Playwright thread

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def unavailable_reason(self) -> str: ...

    @abstractmethod
    def search(self, query: str, limit: int = 10) -> list[dict]: ...

    @abstractmethod
    def get_feed(self, limit: int = 10) -> list[dict]: ...

    @abstractmethod
    def post(self, text: str, media_path: str | None = None) -> dict:
        """Returns {"ok": True, "url": ...} on success."""

    @abstractmethod
    def get_profile(self, username: str) -> dict: ...
