"""YouTube adapter — Data API v3, read-only.

Quota note: search().list costs 100 of the 10,000 free daily units, so the
10-minute SearchCache in tools_social.py really matters here.
"""

from __future__ import annotations

from social.base import SocialAdapter, SocialUnsupported, make_post


class YouTubeAdapter(SocialAdapter):
    platform = "youtube"

    def __init__(self, cfg, profile: dict | None = None):
        self.cfg = cfg
        self.profile = profile or {}
        self._yt = None

    def is_available(self) -> bool:
        return bool(self.cfg.youtube_api_key)

    def unavailable_reason(self) -> str:
        return "YouTube is not set up — add YOUTUBE_API_KEY to .env (see SOCIAL_SETUP.md)."

    def _client(self):
        if self._yt is None:
            from googleapiclient.discovery import build
            self._yt = build("youtube", "v3", developerKey=self.cfg.youtube_api_key,
                             cache_discovery=False)
        return self._yt

    @staticmethod
    def _snippet_post(video_id: str, snippet: dict, stats: dict | None = None) -> dict:
        thumbs = snippet.get("thumbnails", {})
        thumb = (thumbs.get("high") or thumbs.get("medium") or thumbs.get("default") or {})
        stats = stats or {}

        def _int(key):
            val = stats.get(key)
            return int(val) if val is not None else None

        return make_post(
            "youtube",
            author=snippet.get("channelTitle", ""),
            text=snippet.get("title", ""),
            url=f"https://www.youtube.com/watch?v={video_id}",
            timestamp=snippet.get("publishedAt", ""),
            media_urls=[thumb["url"]] if thumb.get("url") else [],
            likes=_int("likeCount"),
            comments=_int("commentCount"),
            views=_int("viewCount"),
        )

    def search(self, query: str, limit: int = 10) -> list[dict]:
        resp = self._client().search().list(
            q=query, part="snippet", type="video", maxResults=min(limit, 50),
        ).execute()
        posts = []
        for item in resp.get("items", []):
            posts.append(self._snippet_post(item["id"]["videoId"], item["snippet"]))
        return posts

    def get_feed(self, limit: int = 10) -> list[dict]:
        resp = self._client().videos().list(
            chart="mostPopular", part="snippet,statistics",
            maxResults=min(limit, 50), regionCode="US",
        ).execute()
        return [self._snippet_post(item["id"], item["snippet"],
                                   item.get("statistics"))
                for item in resp.get("items", [])]

    def post(self, text: str, media_path: str | None = None) -> dict:
        raise SocialUnsupported("The YouTube adapter is read-only — no posting.")

    def get_profile(self, username: str) -> dict:
        handle = username if username.startswith("@") else f"@{username}"
        resp = self._client().channels().list(
            forHandle=handle, part="snippet,statistics",
        ).execute()
        items = resp.get("items", [])
        if not items:
            return {"platform": "youtube", "username": handle, "error": "channel not found"}
        ch = items[0]
        stats = ch.get("statistics", {})
        return {
            "platform": "youtube",
            "username": handle,
            "title": ch["snippet"].get("title"),
            "subscribers": stats.get("subscriberCount"),
            "videos": stats.get("videoCount"),
            "views": stats.get("viewCount"),
            "url": f"https://www.youtube.com/{handle}",
        }
