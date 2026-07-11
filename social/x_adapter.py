"""X/Twitter adapter — official API v2 via tweepy, FREE TIER.

Free tier reality: create_tweet works; search and timelines do NOT (they need
the paid Basic tier). Those methods raise SocialUnsupported with a clear
message instead of pretending.
"""

from __future__ import annotations

from social.base import SocialAdapter, SocialUnsupported

SEARCH_UNAVAILABLE = ("X search/timeline requires the paid Basic API tier; "
                      "the free tier only allows posting. "
                      "Try reddit or youtube for searching instead.")


class XAdapter(SocialAdapter):
    platform = "x"

    def __init__(self, cfg, profile: dict | None = None):
        self.cfg = cfg
        self.profile = profile or {}
        self._client = None

    def is_available(self) -> bool:
        return all((self.cfg.x_api_key, self.cfg.x_api_secret,
                    self.cfg.x_access_token, self.cfg.x_access_token_secret))

    def unavailable_reason(self) -> str:
        return ("X is not set up — add X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN and "
                "X_ACCESS_TOKEN_SECRET to .env (see SOCIAL_SETUP.md).")

    def _api(self):
        if self._client is None:
            import tweepy
            self._client = tweepy.Client(
                consumer_key=self.cfg.x_api_key,
                consumer_secret=self.cfg.x_api_secret,
                access_token=self.cfg.x_access_token,
                access_token_secret=self.cfg.x_access_token_secret,
                wait_on_rate_limit=False,  # a 429 must not freeze the worker 15 min
            )
        return self._client

    def search(self, query: str, limit: int = 10) -> list[dict]:
        raise SocialUnsupported(SEARCH_UNAVAILABLE)

    def get_feed(self, limit: int = 10) -> list[dict]:
        raise SocialUnsupported(SEARCH_UNAVAILABLE)

    def post(self, text: str, media_path: str | None = None) -> dict:
        import tweepy
        if len(text) > 280:
            raise SocialUnsupported(f"Tweet is {len(text)} chars; the limit is 280.")
        media_ids = None
        if media_path:
            # media upload still goes through the v1.1 endpoint
            auth = tweepy.OAuth1UserHandler(
                self.cfg.x_api_key, self.cfg.x_api_secret,
                self.cfg.x_access_token, self.cfg.x_access_token_secret)
            media = tweepy.API(auth).media_upload(media_path)
            media_ids = [media.media_id]
        try:
            resp = self._api().create_tweet(text=text, media_ids=media_ids)
        except tweepy.TooManyRequests:
            raise SocialUnsupported(
                "X rate limit hit (free tier is very limited) — try again later.")
        tweet_id = resp.data["id"]
        handle = (self.profile.get("social", {}).get("x", {}) or {}).get("handle", "i")
        return {"ok": True,
                "url": f"https://x.com/{handle.lstrip('@') or 'i'}/status/{tweet_id}"}

    def get_profile(self, username: str) -> dict:
        import tweepy
        try:
            resp = self._api().get_user(
                username=username.lstrip("@"),
                user_fields=["public_metrics", "description"])
        except tweepy.TooManyRequests:
            raise SocialUnsupported(
                "X free-tier read quota exhausted (it is very low) — try later.")
        if resp.data is None:
            return {"platform": "x", "username": username, "error": "user not found"}
        u = resp.data
        metrics = u.public_metrics or {}
        return {
            "platform": "x",
            "username": f"@{u.username}",
            "name": u.name,
            "bio": u.description,
            "followers": metrics.get("followers_count"),
            "following": metrics.get("following_count"),
            "tweets": metrics.get("tweet_count"),
            "url": f"https://x.com/{u.username}",
        }
