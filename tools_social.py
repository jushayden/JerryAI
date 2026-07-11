"""Reddit + YouTube read-only tools for Pocket Agent (Track C: Social/News).

Per PHASE2_BUILD_PLAN.md's locked Phase 2 scope decision: no posting on any
platform this build, and X/Instagram/TikTok/Facebook reading stays co-drive
only (open the feed in Edge, ask the agent to summarize what's on screen).
Reddit and YouTube each have a free, official, read-only API, so they get
first-class tools instead of scraping. One-time setup: see SOCIAL_SETUP.md.
"""
import asyncio

import config

DIGEST_MAX = config.TOOL_RESULT_MAX
SNIPPET_MAX = 200

_reddit_cache = None
_youtube_cache = None


def _reddit():
    global _reddit_cache
    if _reddit_cache is None:
        if not (config.REDDIT_CLIENT_ID and config.REDDIT_CLIENT_SECRET):
            raise RuntimeError(
                "Reddit is not set up: REDDIT_CLIENT_ID/REDDIT_CLIENT_SECRET "
                "missing. Follow SOCIAL_SETUP.md, then retry.")
        import praw
        _reddit_cache = praw.Reddit(
            client_id=config.REDDIT_CLIENT_ID,
            client_secret=config.REDDIT_CLIENT_SECRET,
            user_agent=config.REDDIT_USER_AGENT or "pocket-agent:v1",
            check_for_async=False,
        )
    return _reddit_cache


def _youtube():
    global _youtube_cache
    if _youtube_cache is None:
        if not config.YOUTUBE_API_KEY:
            raise RuntimeError(
                "YouTube is not set up: YOUTUBE_API_KEY missing. Follow "
                "SOCIAL_SETUP.md, then retry.")
        from googleapiclient.discovery import build
        _youtube_cache = build("youtube", "v3", developerKey=config.YOUTUBE_API_KEY,
                               cache_discovery=False)
    return _youtube_cache


def _one_line(s, limit: int) -> str:
    """Collapse whitespace to a single line and trim to limit chars."""
    s = " ".join(str(s).split())
    return s[:limit] + "…" if len(s) > limit else s


def _reddit_search_sync(query: str, limit: int) -> str:
    posts = list(_reddit().subreddit("all").search(query, limit=limit, sort="relevance"))
    if not posts:
        return f"No Reddit results for '{query}'."
    lines = [f"Reddit search '{query}': {len(posts)} results"]
    for i, s in enumerate(posts, 1):
        author = f"u/{s.author}" if s.author else "[deleted]"
        lines.append(f"{i}. {author} — {_one_line(s.title, SNIPPET_MAX)} "
                     f"({s.score} pts, {s.num_comments} comments) "
                     f"https://reddit.com{s.permalink}")
    out = "\n".join(lines)
    return out[:DIGEST_MAX] + ("…" if len(out) > DIGEST_MAX else "")


def _reddit_feed_sync(subreddits: str, limit: int) -> str:
    target = subreddits.replace(" ", "") or "all"
    posts = list(_reddit().subreddit(target).hot(limit=limit))
    if not posts:
        return f"No posts found in r/{target}."
    lines = [f"r/{target} hot posts:"]
    for i, s in enumerate(posts, 1):
        lines.append(f"{i}. {_one_line(s.title, SNIPPET_MAX)} "
                     f"({s.score} pts) https://reddit.com{s.permalink}")
    out = "\n".join(lines)
    return out[:DIGEST_MAX] + ("…" if len(out) > DIGEST_MAX else "")


def _youtube_search_sync(query: str, limit: int) -> str:
    resp = _youtube().search().list(
        q=query, part="snippet", type="video", maxResults=min(limit, 25),
    ).execute()
    items = resp.get("items", [])
    if not items:
        return f"No YouTube results for '{query}'."
    lines = [f"YouTube search '{query}': {len(items)} results"]
    for i, item in enumerate(items, 1):
        sn = item["snippet"]
        vid = item["id"]["videoId"]
        lines.append(f"{i}. {_one_line(sn['title'], SNIPPET_MAX)} — {sn['channelTitle']} "
                     f"https://www.youtube.com/watch?v={vid}")
    out = "\n".join(lines)
    return out[:DIGEST_MAX] + ("…" if len(out) > DIGEST_MAX else "")


# --- tool functions (never raise; return "Error: ..." strings) ---

async def reddit_search(args: dict) -> str:
    """Search all of Reddit for posts about a topic."""
    try:
        query = str(args.get("query") or "").strip()
        if not query:
            return "Error: query is required."
        limit = int(args.get("limit") or 10)
        return await asyncio.to_thread(_reddit_search_sync, query, limit)
    except Exception as e:
        return f"Error: {e}"


async def reddit_feed(args: dict) -> str:
    """Get hot posts from one or more subreddits."""
    try:
        subreddits = str(args.get("subreddits") or "all")
        limit = int(args.get("limit") or 10)
        return await asyncio.to_thread(_reddit_feed_sync, subreddits, limit)
    except Exception as e:
        return f"Error: {e}"


async def youtube_search(args: dict) -> str:
    """Search YouTube for videos about a topic."""
    try:
        query = str(args.get("query") or "").strip()
        if not query:
            return "Error: query is required."
        limit = int(args.get("limit") or 10)
        return await asyncio.to_thread(_youtube_search_sync, query, limit)
    except Exception as e:
        return f"Error: {e}"


TOOLS: dict[str, dict] = {
    "reddit_search": {
        "schema": {
            "type": "function",
            "function": {
                "name": "reddit_search",
                "description": (
                    "Search all of Reddit for posts about a topic. Read-only. "
                    "Returns author, title, score, comment count, and a link per post."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search topic"},
                        "limit": {"type": "integer",
                                  "description": "Max results, default 10"},
                    },
                    "required": ["query"],
                },
            },
        },
        "fn": reddit_search,
    },
    "reddit_feed": {
        "schema": {
            "type": "function",
            "function": {
                "name": "reddit_feed",
                "description": (
                    "Get hot/trending Reddit posts from one or more subreddits. Read-only."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "subreddits": {
                            "type": "string",
                            "description": ("Comma-separated subreddit names, e.g. "
                                            "'python,LocalLLaMA'. Default: all"),
                        },
                        "limit": {"type": "integer",
                                  "description": "Max posts, default 10"},
                    },
                    "required": [],
                },
            },
        },
        "fn": reddit_feed,
    },
    "youtube_search": {
        "schema": {
            "type": "function",
            "function": {
                "name": "youtube_search",
                "description": (
                    "Search YouTube for videos about a topic. Read-only. "
                    "Returns title, channel, and a link per video."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search topic"},
                        "limit": {"type": "integer",
                                  "description": "Max results, default 10"},
                    },
                    "required": ["query"],
                },
            },
        },
        "fn": youtube_search,
    },
}
