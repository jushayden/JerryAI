"""Live smoke test — needs real Reddit creds and RUN_LIVE_TESTS=1."""

import os

import pytest

from config import load_config
from social.reddit_adapter import RedditAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="RUN_LIVE_TESTS != 1")


def test_reddit_search_live():
    adapter = RedditAdapter(load_config())
    if not adapter.is_available():
        pytest.skip("Reddit creds not configured")
    posts = adapter.search("python", limit=3)
    assert posts and posts[0]["platform"] == "reddit"
    assert posts[0]["url"].startswith("https://reddit.com/")
