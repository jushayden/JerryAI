"""Plain-assert tests for tools_social.py. Run: python test_tools_social.py

Offline-safe: missing-credential and bad-argument paths never touch the
network. If real REDDIT_*/YOUTUBE_API_KEY creds are present in .env, one live
smoke check runs per platform too (skipped with a note otherwise).
"""
import asyncio

import config
import tools_social


async def main():
    had_reddit = (config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET)
    had_youtube = config.YOUTUBE_API_KEY
    live_reddit = bool(config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET)
    live_youtube = bool(config.YOUTUBE_API_KEY)

    try:
        # --- missing query is rejected before any network call ---
        r = await tools_social.reddit_search({})
        assert r == "Error: query is required.", r
        r = await tools_social.youtube_search({"query": "  "})
        assert r == "Error: query is required.", r
        print("PASS missing-query validation")

        # --- friendly missing-credential errors (no network) ---
        tools_social._reddit_cache = None
        config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = "", ""
        r = await tools_social.reddit_search({"query": "test"})
        assert r.startswith("Error: Reddit is not set up"), r
        assert "SOCIAL_SETUP.md" in r, r
        print("PASS reddit missing-credential message")

        tools_social._youtube_cache = None
        config.YOUTUBE_API_KEY = ""
        r = await tools_social.youtube_search({"query": "test"})
        assert r.startswith("Error: YouTube is not set up"), r
        assert "SOCIAL_SETUP.md" in r, r
        print("PASS youtube missing-credential message")

        # --- one_line helper ---
        assert tools_social._one_line("a   b\nc", 100) == "a b c"
        assert tools_social._one_line("x" * 10, 5) == "xxxxx…"
        print("PASS _one_line helper")

        # --- live smoke checks (only if real creds are configured) ---
        if live_reddit:
            config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = had_reddit
            tools_social._reddit_cache = None
            r = await tools_social.reddit_search({"query": "python", "limit": 3})
            assert "reddit.com" in r, r
            print("PASS reddit_search live")
        else:
            print("SKIP reddit_search live (REDDIT_CLIENT_ID/SECRET not set)")

        if live_youtube:
            config.YOUTUBE_API_KEY = had_youtube
            tools_social._youtube_cache = None
            r = await tools_social.youtube_search({"query": "python tutorial", "limit": 3})
            assert "youtube.com/watch" in r, r
            print("PASS youtube_search live")
        else:
            print("SKIP youtube_search live (YOUTUBE_API_KEY not set)")

    finally:
        config.REDDIT_CLIENT_ID, config.REDDIT_CLIENT_SECRET = had_reddit
        config.YOUTUBE_API_KEY = had_youtube
        tools_social._reddit_cache = None
        tools_social._youtube_cache = None

    print("ALL PASS")


if __name__ == "__main__":
    asyncio.run(main())
