"""Recurring / scheduled tasks for Pocket Agent.

Each job is a saved task text plus a recurrence spec. A background loop enqueues due
jobs through the SAME queue -> agent -> report pipeline as any phone task (via an injected
enqueue callback), so scheduled work behaves exactly like a task you typed. Jobs persist
to config.SCHEDULE_PATH (JSON) and survive restarts.

Recurrence specs (parsed by parse_spec):
  daily 08:00 | weekdays 09:30 | weekly mon 07:00 | every 2h | every 30m | hourly
  once 2026-07-12 14:00 | once 14:00
"""
import asyncio
import json
from datetime import datetime, timedelta

import config

_DOW = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DOW_NAME = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

_PARSE_HELP = ("Try: 'daily 08:00', 'weekdays 09:30', 'weekly mon 07:00', "
               "'every 2h', 'every 30m', or 'once 2026-07-12 14:00'.")

_jobs: list[dict] = []


# --- spec parsing ---

def _hhmm(tok: str) -> tuple[int, int]:
    parts = str(tok).split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        raise ValueError(f"bad time '{tok}' — use HH:MM in 24-hour form, e.g. 08:00")
    h, m = int(parts[0]), int(parts[1])
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(f"bad time '{tok}' — hours 0-23, minutes 0-59")
    return h, m


def _parse_once(rest: str, now: datetime) -> datetime:
    rest = rest.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(rest, fmt)
        except ValueError:
            pass
    # bare "HH:MM" -> next occurrence today or tomorrow
    h, m = _hhmm(rest)
    cand = now.replace(hour=h, minute=m, second=0, microsecond=0)
    return cand if cand > now else cand + timedelta(days=1)


def parse_spec(spec: str, now: datetime | None = None) -> dict:
    """Parse a recurrence spec into scheduling fields. Raises ValueError with guidance."""
    now = now or datetime.now()
    s = " ".join(str(spec).lower().split())
    toks = s.split()
    if not toks:
        raise ValueError("empty schedule. " + _PARSE_HELP)
    head = toks[0]

    if head == "daily":
        if len(toks) < 2:
            raise ValueError("daily needs a time, e.g. 'daily 08:00'")
        h, m = _hhmm(toks[1])
        return {"kind": "daily", "time": f"{h:02d}:{m:02d}"}

    if head in ("weekdays", "weekday"):
        if len(toks) < 2:
            raise ValueError("weekdays needs a time, e.g. 'weekdays 09:30'")
        h, m = _hhmm(toks[1])
        return {"kind": "weekdays", "time": f"{h:02d}:{m:02d}"}

    if head == "weekly":
        if len(toks) < 3 or toks[1][:3] not in _DOW:
            raise ValueError("weekly needs a day and time, e.g. 'weekly mon 07:00'")
        h, m = _hhmm(toks[2])
        return {"kind": "weekly", "dow": _DOW[toks[1][:3]], "time": f"{h:02d}:{m:02d}"}

    if head == "hourly":
        return {"kind": "interval", "interval_min": 60}

    if head == "every":
        if len(toks) < 2:
            raise ValueError("every needs an amount, e.g. 'every 2h' or 'every 30m'")
        val, unit = toks[1], "m"
        if val and val[-1] in ("m", "h"):
            unit, val = val[-1], val[:-1]
        if not val.isdigit() or int(val) <= 0:
            raise ValueError("every needs a positive number, e.g. 'every 2h' or 'every 30m'")
        return {"kind": "interval", "interval_min": int(val) * (60 if unit == "h" else 1)}

    if head == "once":
        return {"kind": "once", "at": _parse_once(s[len("once"):], now).timestamp()}

    raise ValueError("couldn't parse that schedule. " + _PARSE_HELP)


# --- next-run computation ---

def next_run_after(job: dict, now: datetime) -> datetime | None:
    """The next occurrence strictly after `now`, or None if a 'once' job is spent."""
    kind = job["kind"]
    if kind == "interval":
        return now + timedelta(minutes=job["interval_min"])
    if kind == "once":
        dt = datetime.fromtimestamp(job["at"])
        return dt if dt > now else None

    h, m = map(int, job["time"].split(":"))
    base = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if kind == "daily":
        return base if base > now else base + timedelta(days=1)
    if kind == "weekdays":
        cand = base if base > now else base + timedelta(days=1)
        while cand.weekday() >= 5:  # skip Sat(5)/Sun(6)
            cand += timedelta(days=1)
        return cand
    if kind == "weekly":
        cand = base + timedelta(days=(job["dow"] - base.weekday()) % 7)
        return cand if cand > now else cand + timedelta(days=7)
    return None


def describe(job: dict) -> str:
    """Human-readable recurrence, e.g. 'weekdays at 08:00'."""
    k = job["kind"]
    if k == "daily":
        return f"every day at {job['time']}"
    if k == "weekdays":
        return f"weekdays at {job['time']}"
    if k == "weekly":
        return f"every {_DOW_NAME[job['dow']]} at {job['time']}"
    if k == "interval":
        mins = job["interval_min"]
        return f"every {mins // 60}h" if mins >= 60 and mins % 60 == 0 else f"every {mins}m"
    if k == "once":
        return f"once at {datetime.fromtimestamp(job['at']).strftime('%b %d %H:%M')}"
    return job.get("spec", "?")


def fmt_next(job: dict) -> str:
    return datetime.fromtimestamp(job["next_run"]).strftime("%a %b %d %H:%M")


# --- persistence + CRUD ---

def load() -> list[dict]:
    global _jobs
    try:
        if config.SCHEDULE_PATH.exists():
            _jobs = json.loads(config.SCHEDULE_PATH.read_text(encoding="utf-8"))
    except Exception:
        _jobs = []
    return _jobs


def save() -> None:
    try:
        config.SCHEDULE_PATH.write_text(json.dumps(_jobs, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass


def _new_id() -> str:
    existing = {j["id"] for j in _jobs}
    n = 1
    while f"s{n}" in existing:
        n += 1
    return f"s{n}"


def jobs() -> list[dict]:
    return _jobs


def add_job(text: str, spec: str, now: datetime | None = None) -> dict:
    """Create + persist a job. Raises ValueError on a bad spec or a past 'once' time."""
    now = now or datetime.now()
    fields = parse_spec(spec, now)
    job = {
        "id": _new_id(),
        "text": text.strip(),
        "spec": " ".join(str(spec).split()),
        "enabled": True,
        "last_run": None,
        "kind": fields["kind"],
        "time": fields.get("time", ""),
        "dow": fields.get("dow", 0),
        "interval_min": fields.get("interval_min", 0),
        "at": fields.get("at", 0.0),
    }
    nxt = next_run_after(job, now)
    if nxt is None:
        raise ValueError("that 'once' time is already in the past.")
    job["next_run"] = nxt.timestamp()
    _jobs.append(job)
    save()
    return job


def remove_job(jid: str) -> bool:
    global _jobs
    before = len(_jobs)
    _jobs = [j for j in _jobs if j["id"] != jid]
    if len(_jobs) != before:
        save()
        return True
    return False


# --- the loop ---

def fire_due(enqueue, log=None, now: datetime | None = None) -> list[str]:
    """Enqueue every due job and roll it forward. `enqueue(text, job_id)`.

    Returns the ids fired. A recurring job is rescheduled to its next occurrence;
    a spent 'once' job is dropped. One catch-up fire per missed job (not a burst).
    """
    now = now or datetime.now()
    fired, changed = [], False
    for job in list(_jobs):
        if not job.get("enabled", True) or job.get("next_run", 0) > now.timestamp():
            continue
        try:
            enqueue(job["text"], job["id"])
        except Exception as e:
            if log:
                log({"event": "schedule_enqueue_failed", "job": job["id"], "error": str(e)})
            continue
        fired.append(job["id"])
        job["last_run"] = now.timestamp()
        nxt = next_run_after(job, now)
        if nxt is None:
            _jobs.remove(job)
        else:
            job["next_run"] = nxt.timestamp()
        changed = True
        if log:
            log({"event": "schedule_fired", "job": job["id"], "text": job["text"]})
    if changed:
        save()
    return fired


async def run(enqueue, tick: int | None = None, owner_ready=None, log=None) -> None:
    """Background scheduler loop. Checks for due jobs every `tick` seconds."""
    tick = tick or getattr(config, "SCHEDULER_TICK", 30)
    while True:
        await asyncio.sleep(tick)
        try:
            if owner_ready is not None and not owner_ready():
                continue
            fire_due(enqueue, log=log)
        except Exception as e:
            if log:
                log({"event": "scheduler_error", "error": str(e)})
