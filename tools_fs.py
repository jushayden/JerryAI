"""Filesystem tools, sandboxed to the workspace/ directory."""

from __future__ import annotations

import time
from pathlib import Path

from agent import ToolSpec
from notify import ToolContext

_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}

MAX_READ_CHARS = 20_000


class SandboxError(Exception):
    pass


def _resolve_safe(workspace: Path, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute() or p.drive:
        raise SandboxError(f"absolute paths not allowed: {rel}")
    for part in p.parts:
        if part.split(".")[0].upper() in _WINDOWS_RESERVED:
            raise SandboxError(f"reserved device name not allowed: {part}")
    resolved = (workspace / p).resolve()
    if not resolved.is_relative_to(workspace.resolve()):
        raise SandboxError(f"path escapes workspace: {rel}")
    return resolved


async def fs_read_file(ctx: ToolContext, path: str, max_chars: int = MAX_READ_CHARS) -> str:
    target = _resolve_safe(ctx.cfg.workspace_dir, path)
    if not target.is_file():
        return f"ERROR: file not found: {path}"
    text = target.read_text(encoding="utf-8", errors="replace")
    if len(text) > max_chars:
        return text[:max_chars] + f"\n...[truncated, {len(text)} chars total]"
    return text


async def fs_write_file(ctx: ToolContext, path: str, content: str) -> str:
    target = _resolve_safe(ctx.cfg.workspace_dir, path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {len(content)} chars to {path}"


async def fs_list_dir(ctx: ToolContext, path: str = ".") -> str:
    target = _resolve_safe(ctx.cfg.workspace_dir, path)
    if not target.is_dir():
        return f"ERROR: not a directory: {path}"
    lines = []
    for entry in sorted(target.iterdir()):
        stat = entry.stat()
        kind = "dir " if entry.is_dir() else "file"
        mtime = time.strftime("%Y-%m-%d %H:%M", time.localtime(stat.st_mtime))
        lines.append(f"{kind}  {entry.name}  {stat.st_size}B  {mtime}")
    return "\n".join(lines) or "(empty)"


async def fs_delete(ctx: ToolContext, path: str) -> str:
    target = _resolve_safe(ctx.cfg.workspace_dir, path)
    if not target.is_file():
        return f"ERROR: file not found: {path}"
    target.unlink()
    return f"Deleted {path}"


def _schema(name: str, description: str, params: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


def register(registry) -> None:
    registry.register(ToolSpec(
        name="fs_read_file",
        func=fs_read_file,
        schema=_schema("fs_read_file", "Read a text file from the agent workspace.",
                       {"path": {"type": "string", "description": "Relative path inside the workspace"}},
                       ["path"]),
    ))
    registry.register(ToolSpec(
        name="fs_write_file",
        func=fs_write_file,
        schema=_schema("fs_write_file", "Write/overwrite a text file in the agent workspace.",
                       {"path": {"type": "string", "description": "Relative path inside the workspace"},
                        "content": {"type": "string", "description": "Full file content to write"}},
                       ["path", "content"]),
    ))
    registry.register(ToolSpec(
        name="fs_list_dir",
        func=fs_list_dir,
        schema=_schema("fs_list_dir", "List files in a workspace directory.",
                       {"path": {"type": "string", "description": "Relative dir path, default '.'"}},
                       []),
    ))
    registry.register(ToolSpec(
        name="fs_delete",
        func=fs_delete,
        gated=True,
        schema=_schema("fs_delete", "Delete a file from the workspace. Requires owner approval.",
                       {"path": {"type": "string", "description": "Relative path inside the workspace"}},
                       ["path"]),
    ))
