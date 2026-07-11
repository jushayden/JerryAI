"""Live smoke test — needs YOUTUBE_API_KEY and RUN_LIVE_TESTS=1.

Each run of test_youtube_search_live costs 100 quota units (of 10k/day).
"""

import os

import pytest

from config import load_config
from social.youtube_adapter import YouTubeAdapter

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_TESTS") != "1", reason="RUN_LIVE_TESTS != 1")


def test_youtube_search_live():
    adapter = YouTubeAdapter(load_config())
    if not adapter.is_available():
        pytest.skip("YOUTUBE_API_KEY not configured")
    posts = adapter.search("python tutorial", limit=3)
    assert posts and posts[0]["platform"] == "youtube"
    assert "youtube.com/watch" in posts[0]["url"]
