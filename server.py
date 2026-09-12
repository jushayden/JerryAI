"""Loopback API for the paired Edge extension, never the public website.

The extension service worker sends X-Pocket-Token. Ordinary website origins
receive no CORS access; secrets never live in content scripts or site code.
"""
import hmac
from urllib.parse import urlsplit

from aiohttp import web

import config
import state

_runner: web.AppRunner | None = None
_task_created_cb = None


def configure(task_created_cb=None) -> None:
    global _task_created_cb
    _task_created_cb = task_created_cb


def _authed(request: web.Request) -> bool:
    token = request.headers.get("X-Pocket-Token", "")
    origin = request.headers.get("Origin", "")
    if origin and not origin.startswith("chrome-extension://"):
        return False
    return bool(config.LOCAL_TOKEN and hmac.compare_digest(
        token.encode("utf-8"), config.LOCAL_TOKEN.encode("utf-8")))


async def _options(request: web.Request) -> web.Response:
    """Allow the extension service worker's CORS preflight, never a web page's."""
    origin = request.headers.get("Origin", "")
    if not origin.startswith("chrome-extension://"):
        return web.Response(status=403)
    return web.Response(status=204, headers={
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Headers": "Content-Type, X-Pocket-Token",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Max-Age": "300",
        "Vary": "Origin",
    })


async def _post_task(request: web.Request) -> web.Response:
    if not _authed(request):
        return web.json_response({"error": "Extension not paired. Check its Options page."}, status=403)
    if config.ALLOWED_CHAT_ID == 0:
        return web.json_response({"error": "Pair your Telegram chat in .env and restart Tora first."}, status=503)
    try:
        body = await request.json()
    except web.HTTPRequestEntityTooLarge:
        raise
    except (ValueError, UnicodeDecodeError):
        return web.json_response({"error": "Expected a JSON object."}, status=400)
    if not isinstance(body, dict):
        return web.json_response({"error": "Expected a JSON object."}, status=400)
    text, url = body.get("text", ""), body.get("url", "")
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        return web.json_response({"error": "Use a prompt between 1 and 4000 characters."}, status=400)
    if not isinstance(url, str) or len(url) > 8192:
        return web.json_response({"error": "Invalid page URL."}, status=400)
    url = url.strip()
    try:
        parsed = urlsplit(url)
        valid_url = parsed.scheme in ("http", "https") and parsed.hostname and not parsed.username and not parsed.password
    except ValueError:
        valid_url = False
    if url and not valid_url:
        return web.json_response({"error": "Only HTTP(S) page URLs are supported."}, status=400)
    if state.queue.qsize() >= config.MAX_QUEUED_TASKS:
        return web.json_response({"error": "Task queue is full. Wait for a task to finish."}, status=429)
    task = state.new_task(text.strip(), origin="badge", source_url=url or None)
    state.log_event({"event": "badge_task", "task": task.id})
    if _task_created_cb is not None:
        try:
            await _task_created_cb(task)
        except Exception as e:
            state.log_event({"event": "badge_notify_failed", "task": task.id, "error": str(e)})
    return web.json_response({"id": task.id}, status=201)


async def _get_task(request: web.Request) -> web.Response:
    if not _authed(request):
        return web.json_response({"error": "Extension not paired."}, status=403)
    task = next((t for t in state.tasks if t.id == request.match_info["id"]), None)
    if task is None:
        return web.json_response({"error": "Unknown task. Tora may have restarted."}, status=404)
    return web.json_response({
        "status": task.status, "result": state.redact_text(task.result) if task.result else None,
        "needs": state.redact_text(task.needs) if task.needs else None,
        "step": state.redact_text(task.steps[-1]) if task.steps else None,
        "steps": len(task.steps), "origin": task.origin,
        "sources": task.sources, "artifacts": len(task.artifacts),
    })


async def _health(request: web.Request) -> web.Response:
    if not _authed(request):
        return web.json_response({"error": "Extension not paired."}, status=403)
    return web.json_response({"status": "ok", "telegram_paired": config.ALLOWED_CHAT_ID != 0})


def create_app() -> web.Application:
    app = web.Application(client_max_size=16 * 1024)
    app.router.add_post("/task", _post_task)
    app.router.add_get("/task/{id}", _get_task)
    app.router.add_get("/health", _health)
    app.router.add_route("OPTIONS", "/task", _options)
    app.router.add_route("OPTIONS", "/task/{id}", _options)
    app.router.add_route("OPTIONS", "/health", _options)
    return app


async def start_server() -> None:
    global _runner
    config.ensure_local_token()
    runner = web.AppRunner(create_app())
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", config.LOCAL_PORT).start()
    except BaseException:
        await runner.cleanup()
        raise
    _runner = runner


async def stop_server() -> None:
    global _runner
    if _runner is not None:
        await _runner.cleanup()
        _runner = None
