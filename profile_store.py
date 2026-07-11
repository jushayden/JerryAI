"""Structured key/value profile store — backs /remember, /memory, /forget, and
remember_fact. Replaces the old raw-append profile_extra.yaml (which just kept
piling up duplicate "key: value" lines with no dedup)."""
import os
import re

import yaml

import config

_KEY_STRIP_RE = re.compile(r"[^a-z0-9_]+")


def _normalize_key(key: str) -> str:
    k = re.sub(r"\s+", "_", key.strip().lower())
    return _KEY_STRIP_RE.sub("_", k).strip("_")


def _migrate_raw_lines(raw: str) -> dict[str, str]:
    """Parse the old raw-append format (possibly duplicate keys — last wins)."""
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, _, v = line.partition(":")
        nk = _normalize_key(k)
        if nk:
            result[nk] = v.strip()
    return result


def _has_duplicate_keys(raw: str) -> bool:
    """PyYAML's safe_load silently keeps the last value for a duplicate key instead
    of raising, so we can't rely on a parse error to detect the old raw-append format."""
    seen = set()
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        nk = _normalize_key(line.partition(":")[0])
        if nk in seen:
            return True
        seen.add(nk)
    return False


def load() -> dict[str, str]:
    path = config.PROFILE_EXTRA_PATH
    if not path.exists():
        return {}
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return {}
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError:
        data = None
    if (not _has_duplicate_keys(raw) and isinstance(data, dict)
            and all(not isinstance(v, (dict, list)) for v in data.values())):
        return {_normalize_key(str(k)): str(v) for k, v in data.items()}
    # Old raw-append format (duplicate keys, or corrupt YAML) — migrate once.
    result = _migrate_raw_lines(raw)
    if result:
        _atomic_write(result)
    return result


def _atomic_write(data: dict[str, str]) -> None:
    config.PROFILE_EXTRA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = config.PROFILE_EXTRA_PATH.with_suffix(".yaml.tmp")
    tmp.write_text(yaml.safe_dump(data, sort_keys=True, allow_unicode=True), encoding="utf-8")
    os.replace(tmp, config.PROFILE_EXTRA_PATH)


def upsert(key: str, value: str) -> str:
    """Set key=value, overwriting any prior value for the same (normalized) key."""
    nk = _normalize_key(key)
    if not nk:
        raise ValueError("empty key")
    data = load()
    data[nk] = str(value).strip()
    _atomic_write(data)
    return nk


def delete(key: str) -> bool:
    nk = _normalize_key(key)
    data = load()
    if nk not in data:
        return False
    del data[nk]
    _atomic_write(data)
    return True


def as_text() -> str:
    """Sorted 'key: value' lines — what read_profile appends for the model to see."""
    data = load()
    return "\n".join(f"{k}: {v}" for k, v in sorted(data.items()))
