"""Telegram digest formatting and delivery for social results."""

from __future__ import annotations

import html
import json
import time
from pathlib import Path

import httpx

from agent import ToolSpec
from notify import Notifier, ToolContext

TELEGRAM_LIMIT = 4096
SNIPPET_LEN = 120
MAX_THUMBS = 10


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on newline boundaries; hard-split only when a single line exceeds limit."""
    if len(text) <= limit:
        return [text] if text else []
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > limit:  # pathological single line
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks


def _engagement_line(post: dict) -> str:
    eng = post.get("engagement") or {}
    bits = []
    for key, icon in (("likes", "👍"), ("comments", "💬"), ("views", "▶")):
        val = eng.get(key)
        if val is not None:
            bits.append(f"{icon}{val}")
    return " ".join(bits)


def format_digest(query: str, results: dict[str, list[dict]],
                  errors: dict[str, str] | None = None) -> list[str]:
    """Results/errors keyed by platform -> list of Telegram-HTML pages."""
    lines = [f"🔎 <b>{html.escape(query)}</b>"]
    for platform, posts in results.items():
        lines.append(f"\n▸ <b>{html.escape(platform)}</b> ({len(posts)})")
        for i, post in enumerate(posts, 1):
            author = html.escape(str(post.get("author", "")))
            snippet = html.escape(str(post.get("text", ""))[:SNIPPET_LEN])
            url = str(post.get("url", ""))
            item = f"{i}. {author} — {snippet}"
            if url:
                item += f' — <a href="{html.escape(url, quote=True)}">link</a>'
            eng = _engagement_line(post)
            if eng:
                item += f"  {eng}"
            lines.append(item)
    for platform, err in (errors or {}).items():
        lines.append(f"\n⚪ <b>{html.escape(platform)}</b>: {html.escape(err)}")
    return split_message("\n".join(lines))


async def download_thumbnails(posts: list[dict], dest: Path,
                              max_n: int = MAX_THUMBS) -> list[Path]:
    """Best-effort thumbnail downloads; failures are skipped silently."""
    dest.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for post in posts:
            if len(paths) >= max_n:
                break
            for url in post.get("media_urls") or []:
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    ctype = resp.headers.get("content-type", "")
                    if not ctype.startswith("image/"):
                        continue
                    ext = ".png" if "png" in ctype else ".jpg"
                    path = dest / f"thumb_{int(time.time() * 1000)}_{len(paths)}{ext}"
                    path.write_bytes(resp.content)
                    paths.append(path)
                    break  # one thumb per post
                except Exception:
                    continue
    return paths


async def send_digest(notifier: Notifier, query: str,
                      results: dict[str, list[dict]],
                      errors: dict[str, str] | None = None,
                      thumbs_dir: Path | None = None,
                      proof_shots: list[Path] | None = None) -> None:
    for page in format_digest(query, results, errors):
        await notifier.send_text(page)
    if thumbs_dir is not None:
        all_posts = [p for posts in results.values() for p in posts]
        thumbs = await download_thumbnails(all_posts, thumbs_dir)
        if thumbs:
            await notifier.send_photos(thumbs, caption=f"Top results: {query}"[:1024])
    for shot in proof_shots or []:
        await notifier.send_photos([shot], caption="Proof screenshot")


async def send_social_digest(ctx: ToolContext, query: str, results_json: str) -> str:
    """Tool wrapper: model passes the JSON it got from social_search calls."""
    try:
        data = json.loads(results_json)
    except json.JSONDecodeError:
        return "ERROR: results_json is not valid JSON — pass the exact social_search output."
    if isinstance(data, list):
        results: dict[str, list[dict]] = {}
        for post in data:
            if isinstance(post, dict):
                results.setdefault(str(post.get("platform", "results")), []).append(post)
    elif isinstance(data, dict):
        results = {k: v for k, v in data.items() if isinstance(v, list)}
    else:
        return "ERROR: results_json must be a list of posts or {platform: [posts]}."
    if not any(results.values()):
        return "ERROR: no posts found in results_json."

    proof = []
    ig = ctx.adapters.get("instagram")
    if ig is not None and "instagram" in results and getattr(ig, "last_screenshot", None):
        proof.append(ig.last_screenshot)
    await send_digest(ctx.notifier, query, results,
                      thumbs_dir=ctx.cfg.downloads_dir / "thumbs",
                      proof_shots=proof)
    return "Digest sent to the owner."


def register(registry) -> None:
    registry.register(ToolSpec(
        name="send_social_digest",
        func=send_social_digest,
        schema={
            "type": "function",
            "function": {
                "name": "send_social_digest",
                "description": "Send a nicely formatted digest of social results to the "
                               "owner, with links and thumbnail images. Pass the exact "
                               "JSON output from social_search/social_get_feed.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string",
                                  "description": "What was searched for"},
                        "results_json": {"type": "string",
                                         "description": "JSON array of posts from social_search"},
                    },
                    "required": ["query", "results_json"],
                },
            },
        },
    ))
