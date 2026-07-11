import json

from pathlib import Path

from report_social import format_digest, send_social_digest, split_message
from social.base import make_post


def test_split_short_message():
    assert split_message("hello") == ["hello"]


def test_split_respects_limit_and_newlines():
    text = "\n".join(f"line {i} " + "x" * 80 for i in range(200))
    chunks = split_message(text)
    assert len(chunks) > 1
    assert all(len(c) <= 4096 for c in chunks)
    # no line was cut in half (all lines short enough to fit)
    reassembled = "\n".join(chunks)
    assert "line 0" in reassembled and "line 199" in reassembled


def test_split_pathological_single_line():
    chunks = split_message("y" * 10_000)
    assert all(len(c) <= 4096 for c in chunks)
    assert sum(len(c) for c in chunks) == 10_000


def test_format_digest_structure_and_escaping():
    results = {
        "reddit": [make_post("reddit", author="u/<bob>", text="Tesla & stuff",
                             url="https://reddit.com/1", likes=5)],
        "youtube": [],
    }
    errors = {"x": "X search requires the paid Basic tier"}
    pages = format_digest("Tesla Model 2", results, errors)
    body = "\n".join(pages)
    assert "Tesla Model 2" in body
    assert "reddit</b> (1)" in body
    assert "&lt;bob&gt;" in body and "<bob>" not in body  # escaped
    assert "Tesla &amp; stuff" in body
    assert 'href="https://reddit.com/1"' in body
    assert "Basic tier" in body
    assert "👍5" in body


class CapturingNotifier:
    def __init__(self):
        self.texts = []
        self.photos = []

    async def send_text(self, text):
        self.texts.append(text)

    async def send_photos(self, paths, caption=""):
        self.photos.append((list(paths), caption))


async def test_send_social_digest_tool(tool_ctx):
    notifier = CapturingNotifier()
    tool_ctx.notifier = notifier
    posts = [make_post("reddit", author="u/a", text="hi", url="https://r.com/1")]
    out = await send_social_digest(tool_ctx, "tesla", json.dumps(posts))
    assert out == "Digest sent to the owner."
    assert notifier.texts and "tesla" in notifier.texts[0]


async def test_send_social_digest_bad_json(tool_ctx):
    out = await send_social_digest(tool_ctx, "q", "{not json")
    assert out.startswith("ERROR")


async def test_send_social_digest_empty(tool_ctx):
    out = await send_social_digest(tool_ctx, "q", "[]")
    assert out.startswith("ERROR")
