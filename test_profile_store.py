"""Plain-assert tests for profile_store.py. Run: python test_profile_store.py"""
import tempfile
from pathlib import Path

import config

# Redirect the profile-extra path to a temp file BEFORE exercising profile_store.
config.PROFILE_EXTRA_PATH = Path(tempfile.mkdtemp(prefix="jerry_profile_test_")) / "profile_extra.yaml"

import profile_store  # noqa: E402

# --- load() on a missing file ---
assert profile_store.load() == {}, profile_store.load()

# --- upsert + normalization ---
profile_store.upsert("Work Authorization", "US citizen")
assert profile_store.load()["work_authorization"] == "US citizen", profile_store.load()

# --- re-upsert with a differently-cased/punctuated key overwrites, no duplicate ---
profile_store.upsert("work-authorization ", "Green card")
data = profile_store.load()
assert data["work_authorization"] == "Green card", data
assert len(data) == 1, data

# --- delete ---
assert profile_store.delete("work_authorization") is True
assert "work_authorization" not in profile_store.load()
assert profile_store.delete("work_authorization") is False  # already gone

# --- as_text() sorted "key: value" lines ---
profile_store.upsert("b_key", "2")
profile_store.upsert("a_key", "1")
assert profile_store.as_text() == "a_key: 1\nb_key: 2", profile_store.as_text()

# --- unicode values round-trip ---
profile_store.upsert("name", "Jürgen Müller — 日本語")
assert profile_store.load()["name"] == "Jürgen Müller — 日本語"

# --- migration: old raw-append format with duplicate keys, last wins, file gets cleaned up ---
config.PROFILE_EXTRA_PATH.write_text(
    "shirt_size: M\nshirt_size: L\nshirt_size: XL\nfavorite_color: blue\n",
    encoding="utf-8",
)
migrated = profile_store.load()
assert migrated["shirt_size"] == "XL", migrated
assert migrated["favorite_color"] == "blue", migrated
# after migration, the file is rewritten in clean structured form (no duplicate raw lines)
raw_after = config.PROFILE_EXTRA_PATH.read_text(encoding="utf-8")
assert raw_after.count("shirt_size") == 1, raw_after

# --- no leftover .tmp file after any upsert/delete ---
profile_store.upsert("x", "y")
profile_store.delete("x")
tmp_path = config.PROFILE_EXTRA_PATH.with_suffix(".yaml.tmp")
assert not tmp_path.exists(), "leftover .tmp file after atomic write"

print("test_profile_store.py: all assertions passed")
