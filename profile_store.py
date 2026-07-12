"""Structured key/value profile store — backs /remember, /memory, /forget, and
remember_fact. Replaces the old raw-append profile_extra.yaml (which just kept
piling up duplicate "key: value" lines with no dedup)."""
import os
import re

import yaml

import config

_KEY_STRIP_RE = re.compile(r"[^a-z0-9_]+")
_SECRET_QUESTION_RE = re.compile(
    r"password|passcode|one[- ]?time|\botp\b|security code|verification code|pin\b|secret",
    re.I,
)
_SKIP_QUESTION_RE = re.compile(
    r"which (file|resume)|file path|attach|upload|captcha|human[- ]?verification|"
    r"verification (?:wall|challenge|check)", re.I,
)
_CONTROL_QUESTION_RE = re.compile(r"stop|cancel|continue (the|this) task|keep going", re.I)
_APPLICATION_KEYS = (
    (re.compile(r"e-?mail", re.I), "email"),
    (re.compile(r"phone|mobile", re.I), "phone"),
    (re.compile(r"pronoun", re.I), "pronouns"),
    (re.compile(r"language skill", re.I), "languages"),
    (re.compile(r"high school.*graduat|graduat.*high school", re.I),
     "high_school_graduation"),
    (re.compile(r"high school", re.I), "high_school"),
    (re.compile(r"offer deadline", re.I), "offer_deadlines"),
    (re.compile(r"final internship", re.I), "final_internship"),
    (re.compile(r"preferred office|preferred location", re.I),
     "preferred_office_locations"),
    (re.compile(r"preferred palantir product", re.I), "preferred_palantir_products"),
    (re.compile(r"AI notetaker|transcribe conversations", re.I),
     "ai_notetaker_consent"),
    (re.compile(r"how you heard|hear about", re.I), "job_source"),
    (re.compile(r"work authori[sz]", re.I), "work_authorization"),
    (re.compile(r"sponsor", re.I), "sponsorship"),
    (re.compile(r"visa", re.I), "visa_status"),
    (re.compile(r"school|college|university", re.I), "school"),
    (re.compile(r"degree", re.I), "degree"),
    (re.compile(r"graduat", re.I), "graduation"),
    (re.compile(r"current company|employer|organization", re.I), "company"),
    (re.compile(r"address", re.I), "address"),
    (re.compile(r"city|location|where do you live", re.I), "location"),
    (re.compile(r"linkedin", re.I), "linkedin"),
    (re.compile(r"github", re.I), "github"),
    (re.compile(r"portfolio|personal website", re.I), "portfolio"),
    (re.compile(r"race", re.I), "race"),
    (re.compile(r"hispanic|latino", re.I), "hispanic"),
    (re.compile(r"disab", re.I), "disabilities"),
    (re.compile(r"veteran", re.I), "veteran_status"),
    (re.compile(r"gender", re.I), "gender"),
    (re.compile(r"salary|compensation", re.I), "salary_expectation"),
)


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


def _application_key(question: str) -> str | None:
    question = str(question or "").strip()
    if (not question or _SECRET_QUESTION_RE.search(question)
            or _SKIP_QUESTION_RE.search(question) or _CONTROL_QUESTION_RE.search(question)):
        return None
    key = next((name for pattern, name in _APPLICATION_KEYS if pattern.search(question)), None)
    if key is None:
        slug = _normalize_key(question)[:80]
        if not slug:
            return None
        key = f"application_{slug}"
    return key


def application_answer(question: str) -> str | None:
    key = _application_key(question)
    return load().get(key) if key else None


def remember_application_answer(question: str, answer: str) -> str | None:
    """Persist a reusable non-secret answer supplied during an application."""
    answer = str(answer or "").strip()
    key = _application_key(question)
    if not key or not answer or re.search(r"\b(stop|cancel)\b", answer, re.I):
        return None
    if key == "company" and re.search(
            r"(?:do(?:n't| not) have|no (?:current )?company|leave (?:it )?(?:blank|empty)|^none$)",
            answer, re.I):
        answer = "(leave blank)"
    return upsert(key, answer)


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
