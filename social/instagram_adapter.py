"""Instagram adapter — Playwright with a persistent logged-in session.

No official API exists for personal accounts, so this scrapes the web UI
using the owner's own session (created once via setup_instagram.py).
ToS note: this violates Instagram's ToS; the design is read-mostly, heavily
throttled, and never retries login challenges. See README.md.

executor="browser": every method runs on the pinned Playwright thread.
"""

from __future__ import annotations

import random
import time

from social.base import LoginChallengeError, SocialAdapter, SocialUnsupported, make_post

FEED_SELECTOR = "article"
CHALLENGE_MARKERS = ("/challenge/", "/accounts/login/", "/accounts/suspended/")


class InstagramAdapter(SocialAdapter):
    platform = "instagram"
    executor = "browser"

    def __init__(self, cfg, cooldowns, profile: dict | None = None):
        self.cfg = cfg
        self.cooldowns = cooldowns
        self.profile = profile or {}
        self._session = None
        self._challenged = False
        self.last_screenshot = None  # proof shot for the digest

    @property
    def session_dir(self):
        return self.cfg.sessions_dir / "instagram"

    def is_available(self) -> bool:
        return (self.session_dir / ".login_ok").exists() and not self._challenged

    def unavailable_reason(self) -> str:
        if self._challenged:
            return ("Instagram is challenging the session — run "
                    "'python setup_instagram.py' and log in again. I will not retry.")
        return "Instagram session not set up — run 'python setup_instagram.py' once."

    # ── browser plumbing (pw thread only) ────────────────────────────────
    def _get_session(self):
        from tools_browser import BrowserSession
        if self._session is None:
            self._session = BrowserSession(self.cfg.downloads_dir / "screens")
            self._session.start(headless=True, user_data_dir=self.session_dir)
        return self._session

    def _throttle(self) -> None:
        allowed, wait = self.cooldowns.check("instagram")
        if not allowed:
            raise SocialUnsupported(
                f"Instagram throttle: wait {int(wait)}s (max "
                f"{self.cfg.instagram_actions_per_hour} actions/hour to protect the account).")
        lo, hi = self.cfg.instagram_min_delay_s
        time.sleep(random.uniform(lo, hi))  # human-like jitter

    def _check_challenge(self, session) -> None:
        url = session.page.url
        if any(marker in url for marker in CHALLENGE_MARKERS):
            shot = None
            try:
                shot = session.screenshot("instagram_challenge")
            except Exception:
                pass
            self._challenged = True
            raise LoginChallengeError(
                "Instagram wants re-verification — run setup_instagram.py manually. "
                "I will NOT retry automatically.", screenshot=shot)

    def _open(self, url: str):
        session = self._get_session()
        self._throttle()
        session.goto(url)
        self._check_challenge(session)
        self.cooldowns.record("instagram")
        return session

    # ── scraping ──────────────────────────────────────────────────────────
    def _scrape_posts(self, session, limit: int) -> list[dict]:
        session.wait_for(FEED_SELECTOR)
        # Scroll a bit so lazy content loads
        for _ in range(2):
            session.page.mouse.wheel(0, 1500)
            session.page.wait_for_timeout(1200)
        self.last_screenshot = session.screenshot("instagram_results")
        posts = []
        for article in session.page.locator(FEED_SELECTOR).all()[:limit]:
            try:
                text = article.inner_text(timeout=3000)[:300]
            except Exception:
                continue
            author = text.split("\n", 1)[0][:60] if text else ""
            link, img = "", []
            try:
                href = article.locator("a[href*='/p/'], a[href*='/reel/']").first \
                    .get_attribute("href", timeout=1500)
                if href:
                    link = f"https://www.instagram.com{href}"
            except Exception:
                pass
            try:
                src = article.locator("img").first.get_attribute("src", timeout=1500)
                if src:
                    img = [src]
            except Exception:
                pass
            posts.append(make_post("instagram", author=author, text=text,
                                   url=link, media_urls=img))
        return posts

    def search(self, query: str, limit: int = 10) -> list[dict]:
        tag = query.strip().lstrip("#").replace(" ", "")
        session = self._open(f"https://www.instagram.com/explore/tags/{tag}/")
        return self._scrape_posts(session, limit)

    def get_feed(self, limit: int = 10) -> list[dict]:
        session = self._open("https://www.instagram.com/")
        return self._scrape_posts(session, limit)

    def post(self, text: str, media_path: str | None = None) -> dict:
        raise SocialUnsupported(
            "Instagram posting via automation is disabled by design (read-mostly "
            "to protect the account). Post manually from your phone.")

    def get_profile(self, username: str) -> dict:
        handle = username.lstrip("@")
        session = self._open(f"https://www.instagram.com/{handle}/")
        try:
            session.wait_for("header")
            header = session.page.locator("header").first.inner_text(timeout=5000)
        except Exception:
            header = "(could not read profile header)"
        self.last_screenshot = session.screenshot(f"instagram_{handle}")
        return {
            "platform": "instagram",
            "username": f"@{handle}",
            "header_text": header[:500],
            "url": f"https://www.instagram.com/{handle}/",
        }
