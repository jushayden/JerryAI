"""Plain-assert tests for bridge.py's pure upload/profile helpers. Run: python test_bridge_uploads.py

Only the pure, synchronous helpers are tested here — not the full async
_on_file/_on_text handlers, which need mocked Update/Context objects that
no test file in this repo currently builds (test_browser.py is a manual,
visible-window test rather than a mocked one; matching that scope here).
"""
import tempfile
import time
from pathlib import Path

import config

# Redirect the profile-extra path to a temp file BEFORE importing bridge
# (bridge.py imports profile_store at module scope).
config.PROFILE_EXTRA_PATH = Path(tempfile.mkdtemp(prefix="jerry_bridge_test_")) / "profile_extra.yaml"

import bridge  # noqa: E402
import profile_store  # noqa: E402


def reset_profile():
    if config.PROFILE_EXTRA_PATH.exists():
        config.PROFILE_EXTRA_PATH.unlink()


# --- _sanitize_filename ---
assert bridge._sanitize_filename("../../evil.txt") == "evil.txt"
assert bridge._sanitize_filename("a/b\\c.txt") == "c.txt"
weird_sanitized = bridge._sanitize_filename('weird<>:"|?*name.txt')
assert weird_sanitized.endswith("name.txt") and weird_sanitized.startswith("weird"), weird_sanitized
assert not any(c in weird_sanitized for c in '<>:"|?*'), weird_sanitized
assert bridge._sanitize_filename("") == "upload"
assert bridge._sanitize_filename("", fallback_ext=".jpg") == "upload.jpg"
assert bridge._sanitize_filename("CON.txt") == "_CON.txt"
assert bridge._sanitize_filename("con") == "_con"
long_name = "x" * 200 + ".txt"
sanitized_long = bridge._sanitize_filename(long_name)
assert len(sanitized_long) <= 150 and sanitized_long.endswith(".txt"), sanitized_long
print("PASS _sanitize_filename")

# --- _dedupe_path ---
tmp_dir = Path(tempfile.mkdtemp(prefix="jerry_dedupe_test_"))
p1 = bridge._dedupe_path(tmp_dir, "resume.pdf")
assert p1 == tmp_dir / "resume.pdf"
p1.write_text("x", encoding="utf-8")
p2 = bridge._dedupe_path(tmp_dir, "resume.pdf")
assert p2 == tmp_dir / "resume (1).pdf", p2
p2.write_text("x", encoding="utf-8")
p3 = bridge._dedupe_path(tmp_dir, "resume.pdf")
assert p3 == tmp_dir / "resume (2).pdf", p3
print("PASS _dedupe_path")

# --- _looks_like_resume ---
assert bridge._looks_like_resume("resume.pdf", None) is True
assert bridge._looks_like_resume("my_resume.docx", None) is True
assert bridge._looks_like_resume("photo.pdf", None) is False  # no resume/cv keyword
assert bridge._looks_like_resume("notes.txt", "here's my resume") is False  # wrong extension
assert bridge._looks_like_resume("cv.pdf", None) is True
print("PASS _looks_like_resume")

# --- _with_upload_context ---
reset_profile()
assert bridge._with_upload_context("summarize it") == "summarize it"  # no last_upload set

real_file = tmp_dir / "resume.pdf"  # exists (created above)
profile_store.upsert("last_upload", str(real_file))
profile_store.upsert("last_upload_ts", str(time.time()))
out = bridge._with_upload_context("summarize it")
assert out == f"summarize it\n(Most recently uploaded file: {real_file})", out
print("PASS _with_upload_context (recent + exists)")

profile_store.upsert("last_upload_ts", str(time.time() - bridge._UPLOAD_RECENCY_SECS - 10))
assert bridge._with_upload_context("summarize it") == "summarize it"  # stale
print("PASS _with_upload_context (stale ts ignored)")

profile_store.upsert("last_upload_ts", str(time.time()))
profile_store.upsert("last_upload", str(tmp_dir / "does_not_exist.pdf"))
assert bridge._with_upload_context("summarize it") == "summarize it"  # deleted/missing file
print("PASS _with_upload_context (missing file ignored)")

print("test_bridge_uploads.py: all assertions passed")
