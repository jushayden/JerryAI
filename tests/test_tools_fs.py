import pytest

from tools_fs import SandboxError, _resolve_safe, fs_delete, fs_list_dir, fs_read_file, fs_write_file


async def test_write_read_roundtrip(tool_ctx):
    assert "Wrote" in await fs_write_file(tool_ctx, "notes.txt", "hello 🚀")
    assert await fs_read_file(tool_ctx, "notes.txt") == "hello 🚀"


async def test_write_creates_subdirs(tool_ctx):
    await fs_write_file(tool_ctx, "a/b/c.txt", "deep")
    assert await fs_read_file(tool_ctx, "a/b/c.txt") == "deep"


async def test_list_dir(tool_ctx):
    await fs_write_file(tool_ctx, "one.txt", "1")
    listing = await fs_list_dir(tool_ctx)
    assert "one.txt" in listing


async def test_delete(tool_ctx):
    await fs_write_file(tool_ctx, "gone.txt", "x")
    assert "Deleted" in await fs_delete(tool_ctx, "gone.txt")
    assert "not found" in await fs_read_file(tool_ctx, "gone.txt")


async def test_read_missing(tool_ctx):
    assert "not found" in await fs_read_file(tool_ctx, "nope.txt")


async def test_truncation(tool_ctx):
    await fs_write_file(tool_ctx, "big.txt", "x" * 25_000)
    out = await fs_read_file(tool_ctx, "big.txt")
    assert "[truncated" in out


def test_sandbox_rejects_dotdot(tool_ctx):
    with pytest.raises(SandboxError):
        _resolve_safe(tool_ctx.cfg.workspace_dir, "..\\..\\secret.txt")


def test_sandbox_rejects_absolute(tool_ctx):
    with pytest.raises(SandboxError):
        _resolve_safe(tool_ctx.cfg.workspace_dir, "C:\\Windows\\system32\\x")


def test_sandbox_rejects_reserved_names(tool_ctx):
    for bad in ("NUL", "con.txt", "COM1"):
        with pytest.raises(SandboxError):
            _resolve_safe(tool_ctx.cfg.workspace_dir, bad)
