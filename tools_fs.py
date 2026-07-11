"""Filesystem / PC tools for Pocket Agent. All paths are sandboxed to config.SANDBOX_ROOT."""
import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

import config


# --- module-level confirmation state (set via configure()) ---

async def _default_confirm(summary: str) -> bool:
    ans = await asyncio.to_thread(input, f"{summary}\nApprove? y/n: ")
    return ans.strip().lower() in ("y", "yes")

confirm_cb = _default_confirm
preauthorized = False
touched: list[Path] = []  # paths written/moved/deleted this task, for report verification


def configure(confirm=None, preauth=False):
    """Set the confirmation callback and preauthorization flag for destructive ops."""
    global confirm_cb, preauthorized
    confirm_cb = confirm if confirm is not None else _default_confirm
    preauthorized = preauth
    touched.clear()


def _safe(path_str: str) -> Path:
    """Expand + resolve a path and verify it stays inside the sandbox root."""
    p = Path(os.path.expandvars(str(path_str))).expanduser().resolve()
    root = config.SANDBOX_ROOT.expanduser().resolve()
    if not p.is_relative_to(root):
        raise ValueError(
            f"path outside sandbox. File access is restricted to {root} — "
            f"use real absolute paths under it (e.g. {root}\\Desktop\\...), "
            f"not placeholders or guessed usernames")
    return p


# --- tool functions (never raise; return "Error: ..." strings) ---

async def read_file(args: dict) -> str:
    try:
        p = _safe(args["path"])
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
        p = _safe(args["path"])
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
        if not preauthorized:
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


async def read_profile(args: dict) -> str:
    try:
        if config.PROFILE_PATH.exists():
            base = config.PROFILE_PATH.read_text(encoding="utf-8")
        else:
            example = config.PROJECT_ROOT / "profile.example.yaml"
            base = ("(demo persona — profile.yaml not filled in yet)\n"
                    + example.read_text(encoding="utf-8"))
        # Merge in facts the user has volunteered via Telegram (/remember).
        if config.PROFILE_EXTRA_PATH.exists():
            extra = config.PROFILE_EXTRA_PATH.read_text(encoding="utf-8").strip()
            if extra:
                base += "\n# --- facts you told me later ---\n" + extra
        return base
    except Exception as e:
        return f"Error: {e}"


async def remember_fact(args: dict) -> str:
    """Persist a fact the user volunteered so the agent can reuse it (digital twin)."""
    try:
        key = str(args.get("key", "")).strip()
        value = str(args.get("value", "")).strip()
        if not key or not value:
            return "Error: remember_fact needs both 'key' and 'value'."
        line = f"{key}: {value}\n"
        with open(config.PROFILE_EXTRA_PATH, "a", encoding="utf-8") as f:
            f.write(line)
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
        # 2) scan the usual folders for matching extensions.
        exts = set()
        for k, v in _PICK_EXT.items():
            if k in hint:
                exts |= v
        if not exts:
            exts = _PICK_EXT["resume"] | _PICK_EXT["image"]
        home = config.SANDBOX_ROOT
        roots = [home / "Documents", home / "Desktop", home / "Downloads",
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
            "Read a text file and return its contents (truncated if long).",
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
            "Delete a file. Requires user approval unless the task was preauthorized.",
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
}
