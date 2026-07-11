"""Social media tools — thin dispatch layer over social/ adapters."""

from __future__ import annotations

import asyncio
import json

from agent import ToolSpec
from notify import ToolContext
from social.base import LoginChallengeError, SocialUnsupported
from state import SearchCache
from tools_browser import run_in_browser_thread

PLATFORMS = ["reddit", "youtube", "x", "instagram"]
MAX_POST_TEXT = 300


def _shrink(posts: list[dict]) -> list[dict]:
    out = []
    for p in posts:
        q = dict(p)
        if isinstance(q.get("text"), str):
            q["text"] = q["text"][:MAX_POST_TEXT]
        out.append(q)
    return out


async def _dispatch(ctx: ToolContext, platform: str, method: str, *args):
    adapter = ctx.adapters.get(platform)
    if adapter is None:
        return f"ERROR: unknown platform '{platform}'. Valid platforms: {', '.join(PLATFORMS)}"
    if not adapter.is_available():
        return adapter.unavailable_reason()

    is_read = method in ("search", "get_feed", "get_profile")
    cache_key = None
    if is_read:
        query = str(args[0]) if args else ""
        limit = args[1] if len(args) > 1 else 0
        cache_key = SearchCache.key(platform, method, query, int(limit or 0))
        cached = ctx.cache.get(cache_key)
        if cached is not None:
            ctx.log.append("adapter_path", task_id=ctx.task_id, platform=platform,
                           method=method, cached=True)
            return cached

    fn = getattr(adapter, method)
    try:
        if adapter.executor == "browser":
            result = await run_in_browser_thread(fn, *args)
        else:
            result = await asyncio.to_thread(fn, *args)
    except SocialUnsupported as e:
        ctx.log.append("adapter_path", task_id=ctx.task_id, platform=platform,
                       method=method, unsupported=str(e))
        return str(e)
    except LoginChallengeError as e:
        ctx.log.append("error", task_id=ctx.task_id, tool=f"social:{platform}",
                       err=str(e))
        if e.screenshot is not None:
            await ctx.notifier.send_photos([e.screenshot], caption=str(e))
        return f"ERROR: {e}"

    ctx.log.append("adapter_path", task_id=ctx.task_id, platform=platform,
                   method=method, path=adapter.executor, cached=False)
    if cache_key is not None:
        ctx.cache.put(cache_key, result)
    return result


def _as_json(value) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str)


async def social_search(ctx: ToolContext, platform: str, query: str, limit: int = 10) -> str:
    result = await _dispatch(ctx, platform, "search", query, int(limit))
    if isinstance(result, list):
        result = _shrink(result)
        if not result:
            return f"No results on {platform} for '{query}'."
    return _as_json(result)


async def social_get_feed(ctx: ToolContext, platform: str, limit: int = 10) -> str:
    result = await _dispatch(ctx, platform, "get_feed", int(limit))
    if isinstance(result, list):
        result = _shrink(result)
        if not result:
            return f"The {platform} feed came back empty."
    return _as_json(result)


async def social_post(ctx: ToolContext, platform: str, text: str,
                      media_path: str | None = None) -> str:
    """GATED — approval already granted by the time this runs."""
    result = await _dispatch(ctx, platform, "post", text, media_path)
    return _as_json(result)


async def social_get_profile(ctx: ToolContext, platform: str, username: str) -> str:
    result = await _dispatch(ctx, platform, "get_profile", username)
    return _as_json(result)


def _schema(name: str, description: str, params: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


_PLATFORM_PARAM = {"type": "string", "enum": PLATFORMS,
                   "description": "Which social platform"}


def register(registry) -> None:
    registry.register(ToolSpec(
        name="social_search",
        func=social_search,
        schema=_schema("social_search",
                       "Search a social media platform for posts/videos about a topic. "
                       "Note: X search is unavailable on the free tier; prefer reddit/youtube.",
                       {"platform": _PLATFORM_PARAM,
                        "query": {"type": "string", "description": "Search topic"},
                        "limit": {"type": "integer", "description": "Max results, default 10"}},
                       ["platform", "query"]),
    ))
    registry.register(ToolSpec(
        name="social_get_feed",
        func=social_get_feed,
        schema=_schema("social_get_feed",
                       "Get the owner's home feed / timeline on a platform.",
                       {"platform": _PLATFORM_PARAM,
                        "limit": {"type": "integer", "description": "Max posts, default 10"}},
                       ["platform"]),
    ))
    registry.register(ToolSpec(
        name="social_post",
        func=social_post,
        gated=True,
        schema=_schema("social_post",
                       "Publish a post on a platform. Requires owner approval. "
                       "Pass the EXACT text to publish.",
                       {"platform": _PLATFORM_PARAM,
                        "text": {"type": "string", "description": "Exact text to publish"},
                        "media_path": {"type": "string",
                                       "description": "Optional path to an image to attach"}},
                       ["platform", "text"]),
    ))
    registry.register(ToolSpec(
        name="social_get_profile",
        func=social_get_profile,
        schema=_schema("social_get_profile",
                       "Get a public profile summary on a platform.",
                       {"platform": _PLATFORM_PARAM,
                        "username": {"type": "string", "description": "Handle, without @"}},
                       ["platform", "username"]),
    ))
