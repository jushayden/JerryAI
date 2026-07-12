"""Filesystem / PC tools for Pocket Agent. All paths are sandboxed to config.SANDBOX_ROOT."""
import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import config
import profile_store


# --- module-level confirmation state (set via configure()) ---

async def _default_confirm(summary: str) -> bool:
    ans = await asyncio.to_thread(input, f"{summary}\nApprove? y/n: ")
    return ans.strip().lower() in ("y", "yes")

confirm_cb = _default_confirm
touched: list[Path] = []  # paths written/moved/deleted this task, for report verification


def configure(confirm=None):
    """Set the confirmation callback for destructive operations."""
    global confirm_cb
    confirm_cb = confirm if confirm is not None else _default_confirm
    touched.clear()


def _safe(path_str: str, *, allow_artifacts: bool = False) -> Path:
    """Expand + resolve a path and verify it stays inside the sandbox root."""
    p = Path(os.path.expandvars(str(path_str))).expanduser().resolve()
    root = config.SANDBOX_ROOT.expanduser().resolve()
    allowed = [root]
    if allow_artifacts:
        allowed.extend((config.UPLOAD_DIR.resolve(), config.DOWNLOAD_DIR.resolve()))
    if not any(p.is_relative_to(base) for base in allowed):
        raise ValueError(
            f"path outside sandbox. File access is restricted to {root} — "
            f"use real absolute paths under it (e.g. {root}\\Desktop\\...), "
            f"not placeholders or guessed usernames")
    return p


# --- tool functions (never raise; return "Error: ..." strings) ---

_PDF_MAX_PAGES = 15


def _read_pdf(p: Path) -> str:
    try:
        import pypdf
    except ImportError:
        return "Error: pypdf is not installed."
    try:
        reader = pypdf.PdfReader(str(p))
    except Exception as e:
        return f"Error: could not open PDF ({e})."
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")
        except Exception:
            pass
    n_pages = len(reader.pages)
    chunks, total, truncated = [], 0, False
    for page in reader.pages[:_PDF_MAX_PAGES]:
        text = ""
        try:
            text = page.extract_text() or ""
        except Exception:
            pass
        chunks.append(text)
        total += len(text)
        if total > config.TOOL_RESULT_MAX:
            truncated = True
            break
    body = "\n".join(chunks).strip()
    if len(body) > config.TOOL_RESULT_MAX:
        body, truncated = body[: config.TOOL_RESULT_MAX], True
    if not body:
        return f"(PDF has {n_pages} page(s) but no extractable text — likely scanned; no OCR available.)"
    if truncated or n_pages > _PDF_MAX_PAGES:
        body += f"\n…(truncated — {n_pages} total pages)"
    return body


async def read_file(args: dict) -> str:
    try:
        p = _safe(args["path"], allow_artifacts=True)
        if p.suffix.lower() == ".pdf":
            return _read_pdf(p)
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text) > config.TOOL_RESULT_MAX:
            text = text[: config.TOOL_RESULT_MAX] + "\n…(truncated)"
        return text
    except Exception as e:
        return f"Error: {e}"


async def write_file(args: dict) -> str:
    try:
        p = _safe(args["path"])
        content = str(args.get("content", ""))
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        touched.append(p)
        return f"wrote {len(content)} chars to {p}"
    except Exception as e:
        return f"Error: {e}"


async def list_dir(args: dict) -> str:
    try:
        p = _safe(args["path"], allow_artifacts=True)
        entries = sorted(p.iterdir(), key=lambda e: e.name.lower())
        names = [e.name + "/" if e.is_dir() else e.name for e in entries]
        if len(names) > 50:
            extra = len(names) - 50
            names = names[:50] + [f"…and {extra} more"]
        return "\n".join(names) if names else "(empty directory)"
    except Exception as e:
        return f"Error: {e}"


async def move_file(args: dict) -> str:
    try:
        src = _safe(args["src"])
        dst = _safe(args["dst"])
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        touched.append(dst)
        return f"moved {src} -> {dst}"
    except Exception as e:
        return f"Error: {e}"


async def delete_file(args: dict) -> str:
    try:
        p = _safe(args["path"])
        if not p.exists():
            return f"Error: {p} does not exist"
        if p.is_dir():
            n = sum(1 for _ in p.rglob("*"))
            summary = f"Delete folder {p} and everything in it ({n} items)"
        else:
            summary = f"Delete file: {p}"
        ok = await confirm_cb(summary)
        if not ok:
            return "User DENIED the deletion. Do not retry."
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        touched.append(p)
        return f"deleted {p}"
    except Exception as e:
        return f"Error: {e}"


async def open_app(args: dict) -> str:
    try:
        name = str(args.get("name", "")).strip().lower()
        allow = {k.lower(): v for k, v in config.APP_ALLOWLIST.items()}
        if name not in allow:
            avail = ", ".join(sorted(config.APP_ALLOWLIST))
            return f"Error: unknown app '{args.get('name', '')}'. Available: {avail}"
        exe = allow[name]
        try:
            subprocess.Popen([exe])
        except FileNotFoundError:
            os.startfile(exe)
        return f"launched {name}"
    except Exception as e:
        return f"Error: {e}"


def _profile_placeholder(key: str, value) -> bool:
    text = str(value or "").strip().lower()
    key = key.lower()
    if key == "email" and ("@example." in text or "alex.demo" in text):
        return True
    if key == "name" and text in {"alex demo", "ada lovelace", "john doe", "jane doe"}:
        return True
    if key == "phone" and ("555-" in text or text in {"1234567890", "0000000000"}):
        return True
    return False


def _clean_profile_value(key: str, value):
    if isinstance(value, dict):
        return {k: _clean_profile_value(str(k), v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clean_profile_value(key, v) for v in value]
    return "(unset - ask the user)" if _profile_placeholder(key, value) else value


def load_profile_data() -> dict:
    """Return the merged profile without shipping demo placeholders as user facts."""
    import yaml
    if not config.PROFILE_PATH.exists():
        return {}
    data = yaml.safe_load(config.PROFILE_PATH.read_text(encoding="utf-8")) or {}
    extra_data = profile_store.load()
    example_path = config.PROJECT_ROOT / "profile.example.yaml"
    example_data = (yaml.safe_load(example_path.read_text(encoding="utf-8")) or {}
                    if example_path.exists() else {})
    for key, value in list(data.items()):
        if key not in extra_data and key in example_data and value == example_data[key]:
            data[key] = "(unset - ask the user)"
    data.update(extra_data)
    return {k: _clean_profile_value(str(k), v) for k, v in data.items()}


async def read_profile(args: dict) -> str:
    try:
        import yaml
        data = load_profile_data()
        if data:
            return yaml.safe_dump(data, sort_keys=True, allow_unicode=True)
        example = config.PROJECT_ROOT / "profile.example.yaml"
        return ("(demo persona — profile.yaml not filled in yet)\n"
                + example.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Error: {e}"


async def remember_fact(args: dict) -> str:
    """Persist a fact the user volunteered so the agent can reuse it (digital twin)."""
    try:
        key = str(args.get("key", "")).strip()
        value = str(args.get("value", "")).strip()
        if not key or not value:
            return "Error: remember_fact needs both 'key' and 'value'."
        profile_store.upsert(key, value)
        return f"Remembered: {key} = {value}"
    except Exception as e:
        return f"Error: {e}"


_PICK_EXT = {
    "resume": {".pdf", ".doc", ".docx"},
    "cv": {".pdf", ".doc", ".docx"},
    "photo": {".jpg", ".jpeg", ".png", ".heic"},
    "headshot": {".jpg", ".jpeg", ".png"},
    "image": {".jpg", ".jpeg", ".png", ".gif", ".webp"},
    "portfolio": {".pdf", ".png", ".jpg", ".jpeg"},
}


async def pick_file(args: dict) -> str:
    """Find the local file to attach. Never guesses among several — returns a numbered
    list for the model to ask the user about. Feeds upload_file."""
    try:
        hint = str(args.get("hint", "")).strip().lower()
        # 1) explicit profile hints (resume_path etc.) win.
        if config.PROFILE_PATH.exists():
            import yaml
            try:
                prof = yaml.safe_load(config.PROFILE_PATH.read_text(encoding="utf-8")) or {}
            except Exception:
                prof = {}
            for key in ("resume_path", f"{hint}_path"):
                val = prof.get(key)
                if val and Path(os.path.expandvars(str(val))).expanduser().is_file():
                    p = Path(os.path.expandvars(str(val))).expanduser()
                    return f"Use this file: {p}"
        # 1b) same, but from facts saved via /remember or an auto-tagged upload
        # (e.g. a Telegram-uploaded resume becomes resume_path automatically).
        extra = profile_store.load()
        for key in ("resume_path", f"{hint}_path"):
            val = extra.get(key)
            if val and Path(os.path.expandvars(val)).expanduser().is_file():
                p = Path(os.path.expandvars(val)).expanduser()
                return f"Use this file: {p}"
        # 2) scan the usual folders for matching extensions, including uploads from Telegram.
        exts = set()
        for k, v in _PICK_EXT.items():
            if k in hint:
                exts |= v
        if not exts:
            exts = _PICK_EXT["resume"] | _PICK_EXT["image"]
        home = config.SANDBOX_ROOT
        roots = [config.UPLOAD_DIR, home / "Documents", home / "Desktop", home / "Downloads",
                 home / "OneDrive" / "Desktop", home / "OneDrive" / "Documents"]
        found = []
        for r in roots:
            if r.is_dir():
                for f in r.iterdir():
                    if f.is_file() and f.suffix.lower() in exts:
                        found.append(f)
        found = sorted(set(found), key=lambda p: p.stat().st_mtime, reverse=True)[:10]
        if not found:
            return (f"No matching files found for '{hint}'. Ask the user for the full path "
                    f"to the file they want to attach.")
        if len(found) == 1:
            return f"Use this file: {found[0]}"
        listing = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(found))
        return ("Multiple candidate files — DO NOT guess. Ask the user which one to use "
                f"(use ask_user), then upload_file with its full path:\n{listing}")
    except Exception as e:
        return f"Error: {e}"


async def list_uploads(args: dict) -> str:
    """List files the user has sent via Telegram, newest first, with full paths."""
    try:
        if not config.UPLOAD_DIR.is_dir():
            return "No files have been uploaded yet."
        entries = [f for f in config.UPLOAD_DIR.iterdir() if f.is_file()]
        entries.sort(key=lambda f: f.stat().st_mtime, reverse=True)
        entries = entries[:30]
        if not entries:
            return "No files have been uploaded yet."
        lines = []
        for f in entries:
            st = f.stat()
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime))
            lines.append(f"{f.name} ({st.st_size / 1024:.0f} KB, {when}) — {f}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: {e}"


async def take_screenshot(args: dict) -> str:
    try:
        import mss
        import mss.tools

        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.SCREENSHOT_DIR / f"shot_{int(time.time())}.png"
        with mss.mss() as sct:
            monitor = sct.monitors[1]  # primary monitor
            shot = sct.grab(monitor)
            mss.tools.to_png(shot.rgb, shot.size, output=str(path))
        return f"screenshot saved: {path}"
    except Exception as e:
        return f"Error: {e}"


def _schema(name: str, description: str, props: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": required,
            },
        },
    }


TOOLS: dict[str, dict] = {
    "read_file": {
        "schema": _schema(
            "read_file",
            "Read a text file (or extract text from a PDF) and return its contents "
            "(truncated if long).",
            {"path": {"type": "string", "description": "Path to the file"}},
            ["path"],
        ),
        "fn": read_file,
    },
    "write_file": {
        "schema": _schema(
            "write_file",
            "Write UTF-8 text to a file, creating parent directories as needed.",
            {
                "path": {"type": "string", "description": "Path to the file"},
                "content": {"type": "string", "description": "Text content to write"},
            },
            ["path", "content"],
        ),
        "fn": write_file,
    },
    "list_dir": {
        "schema": _schema(
            "list_dir",
            "List the entries of a directory. Directories are suffixed with '/'.",
            {"path": {"type": "string", "description": "Path to the directory"}},
            ["path"],
        ),
        "fn": list_dir,
    },
    "move_file": {
        "schema": _schema(
            "move_file",
            "Move or rename a file from src to dst.",
            {
                "src": {"type": "string", "description": "Source path"},
                "dst": {"type": "string", "description": "Destination path"},
            },
            ["src", "dst"],
        ),
        "fn": move_file,
    },
    "delete_file": {
        "schema": _schema(
            "delete_file",
            "Delete a file. Always requires fresh user approval.",
            {"path": {"type": "string", "description": "Path to the file to delete"}},
            ["path"],
        ),
        "fn": delete_file,
    },
    "open_app": {
        "schema": _schema(
            "open_app",
            "Open an allowlisted desktop application by name (e.g. notepad, calculator).",
            {"name": {"type": "string", "description": "App name from the allowlist"}},
            ["name"],
        ),
        "fn": open_app,
    },
    "read_profile": {
        "schema": _schema(
            "read_profile",
            "Read the user's personal profile (name, email, addresses, preferences). "
            "Always use this instead of inventing personal data.",
            {},
            [],
        ),
        "fn": read_profile,
    },
    "take_screenshot": {
        "schema": _schema(
            "take_screenshot",
            "Take a screenshot of the primary monitor and save it as a PNG. "
            "The saved image is sent to the user's phone.",
            {},
            [],
        ),
        "fn": take_screenshot,
    },
    "remember_fact": {
        "schema": _schema(
            "remember_fact",
            "Save a fact the user told you (e.g. a phone number, an answer to a form "
            "question) so you can reuse it later without asking again.",
            {
                "key": {"type": "string", "description": "Short label, e.g. 'work_authorization'"},
                "value": {"type": "string", "description": "The value to remember"},
            },
            ["key", "value"],
        ),
        "fn": remember_fact,
    },
    "pick_file": {
        "schema": _schema(
            "pick_file",
            "Find a local file to attach (resume, photo, etc.). Returns the path if it's "
            "unambiguous, or a numbered list to ask the user about. Never picks blindly.",
            {"hint": {"type": "string", "description": "What kind of file, e.g. 'resume' or 'headshot'"}},
            ["hint"],
        ),
        "fn": pick_file,
    },
    "list_uploads": {
        "schema": _schema(
            "list_uploads",
            "List files the user has sent via Telegram (documents/photos/audio/video), "
            "newest first, with full paths. Use when pick_file doesn't find the right file "
            "or the user refers to something sent earlier (e.g. 'the file from last week').",
            {},
            [],
        ),
        "fn": list_uploads,
    },
}
