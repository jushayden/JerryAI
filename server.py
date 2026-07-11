"""Local HTTP endpoint for the Edge J-badge extension.

POST /task {"text": "...", "url": "https://current-tab..."} -> {"id": ...}
GET  /task/{id}                                             -> {"status", "result", "steps"}
Bound to 127.0.0.1 only. Requests must carry X-Pocket-Token matching config.LOCAL_TOKEN.
"""
from aiohttp import web

import config
import state

_runner: web.AppRunner | None = None


def _cors(resp: web.Response) -> web.Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Pocket-Token"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return resp


def _authed(request: web.Request) -> bool:
    return request.headers.get("X-Pocket-Token") == config.LOCAL_TOKEN


async def _options(request: web.Request) -> web.Response:
    return _cors(web.Response())


async def _post_task(request: web.Request) -> web.Response:
    if not _authed(request):
        return _cors(web.json_response({"error": "bad token"}, status=403))
    try:
        body = await request.json()
    except Exception:
        return _cors(web.json_response({"error": "bad json"}, status=400))
    text = str(body.get("text", "")).strip()
    if not text:
        return _cors(web.json_response({"error": "empty prompt"}, status=400))
    url = str(body.get("url", "")).strip()
    if url and not url.startswith("edge://"):
        text = f"{text}\n(The user is currently looking at this page: {url} — start there if relevant.)"
    task = state.new_task(text)
    state.log_event({"event": "badge_task", "task": task.id})
    return _cors(web.json_response({"id": task.id}))


async def _get_task(request: web.Request) -> web.Response:
    if not _authed(request):
        return _cors(web.json_response({"error": "bad token"}, status=403))
    tid = request.match_info["id"]
    task = next((t for t in state.tasks if t.id == tid), None)
    if task is None:
        return _cors(web.json_response({"error": "unknown task"}, status=404))
    return _cors(web.json_response({
        "status": task.status,
        "result": task.result,
        "needs": task.needs,
        "step": task.steps[-1] if task.steps else None,
        "steps": len(task.steps),
    }))


async def start_server() -> None:
    global _runner
    app = web.Application()
    app.router.add_post("/task", _post_task)
    app.router.add_get("/task/{id}", _get_task)
    app.router.add_route("OPTIONS", "/task", _options)
    app.router.add_route("OPTIONS", "/task/{id}", _options)
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, "127.0.0.1", config.LOCAL_PORT)
    await site.start()


async def stop_server() -> None:
    if _runner is not None:
        await _runner.cleanup()
