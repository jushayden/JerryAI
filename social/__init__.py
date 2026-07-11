"""Adapter registry."""

from __future__ import annotations

from config import Config, load_profile


def build_adapters(cfg: Config, cooldowns) -> dict:
    from social.instagram_adapter import InstagramAdapter
    from social.reddit_adapter import RedditAdapter
    from social.x_adapter import XAdapter
    from social.youtube_adapter import YouTubeAdapter

    profile = load_profile()
    return {
        "reddit": RedditAdapter(cfg, profile),
        "youtube": YouTubeAdapter(cfg, profile),
        "x": XAdapter(cfg, profile),
        "instagram": InstagramAdapter(cfg, cooldowns, profile),
    }
