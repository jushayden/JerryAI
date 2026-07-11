"""Reddit adapter — official API via praw (sync; runs in a worker thread)."""

from __future__ import annotations

from social.base import SocialAdapter, SocialUnsupported, make_post


class RedditAdapter(SocialAdapter):
    platform = "reddit"

    def __init__(self, cfg, profile: dict | None = None):
        self.cfg = cfg
        self.profile = profile or {}
        self._reddit = None

    def is_available(self) -> bool:
        return all((self.cfg.reddit_client_id, self.cfg.reddit_client_secret,
                    self.cfg.reddit_user_agent))

    def unavailable_reason(self) -> str:
        return ("Reddit is not set up — add REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET "
                "and REDDIT_USER_AGENT to .env (see SOCIAL_SETUP.md).")

    def _client(self):
        # Constructed lazily INSIDE the worker thread (praw warns if built on a
        # thread with a running asyncio loop).
        if self._reddit is None:
            import praw
            kwargs = dict(
                client_id=self.cfg.reddit_client_id,
                client_secret=self.cfg.reddit_client_secret,
                user_agent=self.cfg.reddit_user_agent,
                check_for_async=False,
            )
            if self.cfg.reddit_username and self.cfg.reddit_password:
                kwargs.update(username=self.cfg.reddit_username,
                              password=self.cfg.reddit_password)
            self._reddit = praw.Reddit(**kwargs)
        return self._reddit

    @staticmethod
    def _to_post(s) -> dict:
        text = s.title
        body = getattr(s, "selftext", "") or ""
        if body:
            text += " — " + body[:300]
        media = []
        url = getattr(s, "url", "")
        if url and any(url.lower().endswith(ext) for ext in (".jpg", ".jpeg", ".png", ".gif")):
            media.append(url)
        return make_post(
            "reddit",
            author=f"u/{s.author}" if s.author else "[deleted]",
            text=text,
            url=f"https://reddit.com{s.permalink}",
            timestamp=str(getattr(s, "created_utc", "")),
            media_urls=media,
            likes=getattr(s, "score", None),
            comments=getattr(s, "num_comments", None),
        )

    def search(self, query: str, limit: int = 10) -> list[dict]:
        results = self._client().subreddit("all").search(query, limit=limit,
                                                         sort="relevance")
        return [self._to_post(s) for s in results]

    def get_feed(self, limit: int = 10) -> list[dict]:
        subs = (self.profile.get("social", {}).get("reddit", {}) or {}).get("subreddits") or []
        target = "+".join(subs) if subs else "all"
        return [self._to_post(s) for s in self._client().subreddit(target).hot(limit=limit)]

    def post(self, text: str, media_path: str | None = None) -> dict:
        if not (self.cfg.reddit_username and self.cfg.reddit_password):
            raise SocialUnsupported(
                "Posting to Reddit needs REDDIT_USERNAME and REDDIT_PASSWORD in .env.")
        # Convention: "r/subreddit: title | body" or "r/subreddit: title"
        if not text.lower().startswith("r/") or ":" not in text:
            raise SocialUnsupported(
                'Reddit posts must be formatted "r/subreddit: title | body".')
        sub_part, rest = text.split(":", 1)
        subreddit = sub_part[2:].strip()
        title, _, body = rest.partition("|")
        submission = self._client().subreddit(subreddit).submit(
            title=title.strip(), selftext=body.strip())
        return {"ok": True, "url": f"https://reddit.com{submission.permalink}"}

    def get_profile(self, username: str) -> dict:
        u = self._client().redditor(username.lstrip("u/"))
        return {
            "platform": "reddit",
            "username": f"u/{u.name}",
            "link_karma": getattr(u, "link_karma", None),
            "comment_karma": getattr(u, "comment_karma", None),
            "url": f"https://reddit.com/u/{u.name}",
        }
