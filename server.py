"""Local HTTP endpoint for the Edge J-badge extension.

POST /task {"text": "...", "url": "https://current-tab..."} -> {"id": ...}
GET  /task/{id}                                             -> {"status", "result", "steps"}
Bound to 127.0.0.1 only. Requests must carry X-Pocket-Token matching config.LOCAL_TOKEN.
"""
from aiohttp import web
from pathlib import Path
import re
import time

import config
import profile_store
import state

_runner: web.AppRunner | None = None
_task_created_cb = None


def configure(task_created_cb=None) -> None:
    """Inject the Telegram notifier without introducing a bridge/server import cycle."""
    global _task_created_cb
    _task_created_cb = task_created_cb


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
    if url.startswith("edge://"):
        url = ""
    tab_token = str(body.get("tab_token", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,100}", tab_token):
        tab_token = ""
    attachments = []
    upload_root = config.UPLOAD_DIR.resolve()
    for raw in body.get("attachments", []) if isinstance(body.get("attachments", []), list) else []:
        try:
            path = Path(str(raw)).resolve()
            if path.is_relative_to(upload_root) and path.is_file():
                attachments.append(str(path))
        except Exception:
            continue
    task = state.new_task(
        text, origin="badge", source_url=url or None,
        source_tab_token=tab_token or None, attachments=attachments)
    state.log_event({"event": "badge_task", "task": task.id})
    if _task_created_cb is not None:
        try:
            await _task_created_cb(task)
        except Exception as e:
            state.log_event({"event": "badge_notify_failed", "task": task.id,
                             "error": str(e)})
    return _cors(web.json_response({"id": task.id}))


async def _post_upload(request: web.Request) -> web.Response:
    """Receive one user-selected J-panel attachment into Jerry's upload vault."""
    if not _authed(request):
        return _cors(web.json_response({"error": "bad token"}, status=403))
    try:
        reader = await request.multipart()
        part = await reader.next()
        if part is None or part.name != "file" or not part.filename:
            return _cors(web.json_response({"error": "missing file"}, status=400))
        import bridge  # reuse the same hardened filename rules as Telegram uploads
        config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        safe_name = bridge._sanitize_filename(part.filename)
        dest = bridge._dedupe_path(config.UPLOAD_DIR, safe_name)
        size = 0
        with open(dest, "wb") as output:
            while True:
                chunk = await part.read_chunk(size=256 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > config.LOCAL_UPLOAD_MAX_BYTES:
                    raise ValueError(
                        f"file exceeds the {config.LOCAL_UPLOAD_MAX_BYTES // 1_048_576} MB limit")
                output.write(chunk)
        state.log_event({"event": "badge_upload_saved", "path": str(dest), "size": size})
        profile_store.upsert("last_upload", str(dest))
        profile_store.upsert("last_upload_ts", str(time.time()))
        if bridge._looks_like_resume(safe_name, ""):
            profile_store.upsert("resume_path", str(dest))
        return _cors(web.json_response({
            "name": safe_name, "path": str(dest.resolve()), "size": size,
        }))
    except ValueError as e:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        return _cors(web.json_response({"error": str(e)}, status=413))
    except Exception as e:
        try:
            dest.unlink(missing_ok=True)
        except Exception:
            pass
        state.log_event({"event": "badge_upload_failed", "error": str(e)})
        return _cors(web.json_response({"error": f"upload failed: {e}"}, status=500))


async def _get_task(request: web.Request) -> web.Response:
    if not _authed(request):
        return _cors(web.json_response({"error": "bad token"}, status=403))
    tid = request.match_info["id"]
    task = next((t for t in state.tasks if t.id == tid), None)
    if task is None:
        return _cors(web.json_response({"error": "unknown task"}, status=404))
    import bridge  # lazy: reuse the phone card's step prettifier (no import cycle)
    return _cors(web.json_response({
        "status": task.status,
        "result": task.result,
        "needs": task.needs,
        "step": task.steps[-1] if task.steps else None,
        "steps": len(task.steps),
        "recent_steps": [bridge._friendly_step(s) for s in task.steps[-5:]],
        "origin": task.origin,
        "sources": task.sources,
        "artifacts": len(task.artifacts),
    }))


async def _cancel_task(request: web.Request) -> web.Response:
    if not _authed(request):
        return _cors(web.json_response({"error": "bad token"}, status=403))
    import bridge
    stopped = await bridge.cancel_task(request.match_info["id"])
    if not stopped:
        return _cors(web.json_response({"error": "task is not running or queued"}, status=409))
    return _cors(web.json_response({"status": "cancelled"}))


async def start_server() -> None:
    global _runner
    app = web.Application()
    app.router.add_post("/task", _post_task)
    app.router.add_post("/upload", _post_upload)
    app.router.add_get("/task/{id}", _get_task)
    app.router.add_post("/task/{id}/cancel", _cancel_task)
    app.router.add_route("OPTIONS", "/task", _options)
    app.router.add_route("OPTIONS", "/upload", _options)
    app.router.add_route("OPTIONS", "/task/{id}", _options)
    app.router.add_route("OPTIONS", "/task/{id}/cancel", _options)
    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, "127.0.0.1", config.LOCAL_PORT)
    await site.start()


async def stop_server() -> None:
    if _runner is not None:
        await _runner.cleanup()
