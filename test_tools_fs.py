"""Plain-assert tests for tools_fs.py. Run: python test_tools_fs.py"""
import asyncio
import os
import shutil
import uuid
from pathlib import Path

import config
import tools_fs


async def main():
    tmp = (config.SANDBOX_ROOT / f"_pocket_agent_test_{uuid.uuid4().hex[:8]}").resolve()
    tmp.mkdir(parents=True)
    try:
        # --- write / read round-trip ---
        f = tmp / "sub" / "hello.txt"
        r = await tools_fs.write_file({"path": str(f), "content": "hello pocket agent"})
        assert r.startswith("wrote 18 chars to "), r
        assert f.read_text(encoding="utf-8") == "hello pocket agent"
        r = await tools_fs.read_file({"path": str(f)})
        assert r == "hello pocket agent", r
        print("PASS write/read round-trip")

        # --- read truncation ---
        big = tmp / "big.txt"
        big.write_text("x" * (config.TOOL_RESULT_MAX + 500), encoding="utf-8")
        r = await tools_fs.read_file({"path": str(big)})
        assert len(r) <= config.TOOL_RESULT_MAX + 20 and "truncated" in r, len(r)
        print("PASS read truncation")

        # --- list_dir ---
        r = await tools_fs.list_dir({"path": str(tmp)})
        assert "sub/" in r and "big.txt" in r, r
        print("PASS list_dir (dirs suffixed with /)")

        # --- move ---
        dst = tmp / "moved.txt"
        r = await tools_fs.move_file({"src": str(f), "dst": str(dst)})
        assert "moved" in r and not f.exists() and dst.exists(), r
        print("PASS move_file")

        # --- sandbox escapes (reads AND writes) ---
        for bad in [r"C:\Windows\system32\drivers\etc\hosts", str(tmp / ".." / ".." / ".." / ".." / ".." / ".." / "Windows" / "x")]:
            r = await tools_fs.read_file({"path": bad})
            r2 = await tools_fs.write_file({"path": bad, "content": "nope"})
            r3 = await tools_fs.delete_file({"path": bad})
            for res in (r, r2, r3):
                assert res.startswith("Error") and "outside sandbox" in res, (bad, res)
        print("PASS sandbox escape rejected for read/write/delete")

        # --- delete: denied leaves file ---
        async def deny(summary):
            return False

        async def allow(summary):
            return True

        tools_fs.configure(confirm=deny)
        r = await tools_fs.delete_file({"path": str(dst)})
        assert r == "User DENIED the deletion. Do not retry.", r
        assert dst.exists()
        print("PASS delete_file denied -> file kept")

        # --- delete: approved deletes ---
        tools_fs.configure(confirm=allow)
        r = await tools_fs.delete_file({"path": str(dst)})
        assert r.startswith("deleted") and not dst.exists(), r
        print("PASS delete_file approved -> file deleted")

        # --- delete: every deletion requires a fresh confirmation ---
        again = tmp / "again.txt"
        again.write_text("bye", encoding="utf-8")
        tools_fs.configure(confirm=deny)
        r = await tools_fs.delete_file({"path": str(again)})
        assert r == "User DENIED the deletion. Do not retry." and again.exists(), r
        tools_fs.configure(confirm=allow)
        r = await tools_fs.delete_file({"path": str(again)})
        assert r.startswith("deleted") and not again.exists(), r
        tools_fs.configure()  # reset to defaults
        print("PASS delete_file always requires fresh confirmation")

        # --- open_app: unknown app must NOT launch anything ---
        r = await tools_fs.open_app({"name": "definitely_not_an_app"})
        assert r.startswith("Error: unknown app 'definitely_not_an_app'. Available: "), r
        assert "notepad" in r and "calculator" in r
        print("PASS open_app rejects unknown app (nothing launched)")

        # --- remember_fact writes valid YAML and updates existing keys ---
        r = await tools_fs.remember_fact({"key": "work authorization", "value": "US citizen: yes"})
        assert r.startswith("Remembered: work authorization"), r
        facts = await tools_fs.remember_fact({"key": "work authorization", "value": "US citizen: confirmed"})
        assert facts.startswith("Remembered:"), facts
        saved = config.PROFILE_EXTRA_PATH.read_text(encoding="utf-8")
        assert "US citizen: confirmed" in saved and saved.count("work authorization") == 1, saved
        print("PASS remember_fact writes valid YAML without duplicate keys")

        # --- read_profile refuses missing real profile ---
        r = await tools_fs.read_profile({})
        if not config.PROFILE_PATH.exists():
            assert r.startswith("Error: profile.yaml is missing."), r[:100]
            print("PASS read_profile refuses missing real profile")
        else:
            assert not r.startswith("Error"), r
            print("PASS read_profile (real profile.yaml present)")

        # --- take_screenshot: real PNG > 10KB (requires a desktop session) ---
        if os.name != "nt" and not os.environ.get("DISPLAY"):
            print("SKIP take_screenshot (no desktop display in this headless runner)")
        else:
            r = await tools_fs.take_screenshot({})
            assert r.startswith("screenshot saved: "), r
            shot = Path(r.removeprefix("screenshot saved: "))
            assert shot.exists() and shot.suffix == ".png"
            size = shot.stat().st_size
            assert size > 10 * 1024, f"screenshot too small: {size} bytes"
            assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
            shot.unlink()
            print(f"PASS take_screenshot ({size} bytes PNG, deleted after check)")

        # --- registry contract shape ---
        for name, entry in tools_fs.TOOLS.items():
            assert set(entry) >= {"schema", "fn"}, name
            fn = entry["schema"]["function"]
            assert entry["schema"]["type"] == "function"
            assert fn["name"] == name and fn["description"] and "parameters" in fn
            assert asyncio.iscoroutinefunction(entry["fn"]), name
        print("PASS TOOLS registry contract shape")

        print("\nALL TESTS PASSED")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
