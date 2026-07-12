import json

from social.base import SocialAdapter, SocialUnsupported, make_post
from social.x_adapter import SEARCH_UNAVAILABLE, XAdapter
from social.youtube_adapter import YouTubeAdapter
from tools_social import social_get_feed, social_get_profile, social_post, social_search


class FakeAdapter(SocialAdapter):
    platform = "reddit"  # pretend

    def __init__(self, available=True, posts=None):
        self.available = available
        self.posts = posts if posts is not None else [
            make_post("reddit", author="u/alice", text="Tesla Model 2 spotted",
                      url="https://reddit.com/r/x/1", likes=42, comments=7),
        ]
        self.search_calls = 0

    def is_available(self):
        return self.available

    def unavailable_reason(self):
        return "Fake is not set up — see SOCIAL_SETUP.md."

    def search(self, query, limit=10):
        self.search_calls += 1
        return self.posts

    def get_feed(self, limit=10):
        return self.posts

    def post(self, text, media_path=None):
        return {"ok": True, "url": "https://reddit.com/r/x/posted"}

    def get_profile(self, username):
        return {"platform": "reddit", "username": f"u/{username}"}


async def test_search_returns_normalized_json(tool_ctx):
    tool_ctx.adapters["reddit"] = FakeAdapter()
    out = await social_search(tool_ctx, "reddit", "tesla", 5)
    posts = json.loads(out)
    assert posts[0]["author"] == "u/alice"
    assert posts[0]["engagement"]["likes"] == 42


async def test_unknown_platform(tool_ctx):
    out = await social_search(tool_ctx, "myspace", "tesla")
    assert "unknown platform" in out and "youtube" in out


async def test_unavailable_platform_friendly_message(tool_ctx):
    tool_ctx.adapters["reddit"] = FakeAdapter(available=False)
    out = await social_search(tool_ctx, "reddit", "tesla")
    assert "SOCIAL_SETUP.md" in out


async def test_search_cached_on_second_call(tool_ctx):
    fake = FakeAdapter()
    tool_ctx.adapters["reddit"] = fake
    await social_search(tool_ctx, "reddit", "tesla", 5)
    await social_search(tool_ctx, "reddit", "tesla", 5)
    assert fake.search_calls == 1  # second hit came from cache
    events = (tool_ctx.cfg.state_dir / "events.jsonl").read_text(encoding="utf-8")
    assert '"cached": true' in events


async def test_long_post_text_truncated(tool_ctx):
    tool_ctx.adapters["reddit"] = FakeAdapter(
        posts=[make_post("reddit", text="y" * 2000)])
    posts = json.loads(await social_search(tool_ctx, "reddit", "q"))
    assert len(posts[0]["text"]) == 300


async def test_empty_results_message(tool_ctx):
    tool_ctx.adapters["reddit"] = FakeAdapter(posts=[])
    out = await social_search(tool_ctx, "reddit", "obscure")
    assert "No results" in out


async def test_post_returns_url(tool_ctx):
    tool_ctx.adapters["reddit"] = FakeAdapter()
    out = await social_post(tool_ctx, "reddit", "hello")
    assert json.loads(out)["ok"] is True


async def test_get_feed_and_profile(tool_ctx):
    tool_ctx.adapters["reddit"] = FakeAdapter()
    feed = json.loads(await social_get_feed(tool_ctx, "reddit", 5))
    assert feed[0]["platform"] == "reddit"
    prof = json.loads(await social_get_profile(tool_ctx, "reddit", "alice"))
    assert prof["username"] == "u/alice"


async def test_social_unsupported_becomes_friendly_string(tool_ctx):
    class Unsupported(FakeAdapter):
        def search(self, query, limit=10):
            raise SocialUnsupported("This tier cannot search.")

    tool_ctx.adapters["reddit"] = Unsupported()
    out = await social_search(tool_ctx, "reddit", "q")
    assert out == "This tier cannot search."


# ── X adapter (no creds / no network needed) ──────────────────────────────

def x_with_creds(tmp_config):
    import dataclasses
    return dataclasses.replace(
        tmp_config, x_api_key="k", x_api_secret="s",
        x_access_token="t", x_access_token_secret="ts")


def test_x_unavailable_without_creds(tmp_config):
    adapter = XAdapter(tmp_config)
    assert not adapter.is_available()
    assert "X_API_KEY" in adapter.unavailable_reason()


def test_x_search_unavailable_on_free_tier(tmp_config):
    adapter = XAdapter(x_with_creds(tmp_config))
    assert adapter.is_available()
    try:
        adapter.search("q")
        raise AssertionError("should have raised")
    except SocialUnsupported as e:
        assert str(e) == SEARCH_UNAVAILABLE


def test_x_rejects_over_280_chars(tmp_config):
    adapter = XAdapter(x_with_creds(tmp_config))
    try:
        adapter.post("x" * 281)
        raise AssertionError("should have raised")
    except SocialUnsupported as e:
        assert "280" in str(e)


def test_youtube_post_is_read_only(tmp_config):
    import dataclasses
    adapter = YouTubeAdapter(dataclasses.replace(tmp_config, youtube_api_key="k"))
    try:
        adapter.post("text")
        raise AssertionError("should have raised")
    except SocialUnsupported as e:
        assert "read-only" in str(e)
