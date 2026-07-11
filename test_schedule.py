"""Offline tests for schedule.py — deterministic (injected `now`), no Telegram/network.
Run: python test_schedule.py"""
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import config
import schedule

NOW = datetime(2026, 7, 11, 10, 30, 0)  # a fixed reference time


def main():
    # --- parse_spec: each supported kind ---
    assert schedule.parse_spec("daily 08:00") == {"kind": "daily", "time": "08:00"}
    assert schedule.parse_spec("weekdays 9:30") == {"kind": "weekdays", "time": "09:30"}
    assert schedule.parse_spec("weekly mon 07:00") == {"kind": "weekly", "dow": 0, "time": "07:00"}
    assert schedule.parse_spec("every 2h") == {"kind": "interval", "interval_min": 120}
    assert schedule.parse_spec("every 30m") == {"kind": "interval", "interval_min": 30}
    assert schedule.parse_spec("hourly") == {"kind": "interval", "interval_min": 60}
    once = schedule.parse_spec("once 2026-12-25 09:00")
    assert once["kind"] == "once" and datetime.fromtimestamp(once["at"]).month == 12
    print("PASS parse_spec (all kinds)")

    # --- parse_spec: bad input raises with guidance ---
    for bad in ["", "blah", "daily 99:99", "weekly xyz 08:00", "every 0m", "daily"]:
        try:
            schedule.parse_spec(bad)
            assert False, f"expected ValueError for {bad!r}"
        except ValueError:
            pass
    print("PASS parse_spec rejects bad specs")

    # --- next_run_after: daily before vs after the time-of-day ---
    d_future = schedule.next_run_after({"kind": "daily", "time": "22:00"}, NOW)
    assert d_future.date() == NOW.date() and d_future.hour == 22, d_future
    d_tmrw = schedule.next_run_after({"kind": "daily", "time": "08:00"}, NOW)
    assert d_tmrw.date() == (NOW + timedelta(days=1)).date() and d_tmrw.hour == 8, d_tmrw
    print("PASS next_run_after daily (today if future, else tomorrow)")

    # --- weekdays never lands on a weekend ---
    wd = schedule.next_run_after({"kind": "weekdays", "time": "09:00"}, NOW)
    assert wd.weekday() < 5 and wd.hour == 9 and wd > NOW, wd
    print("PASS next_run_after weekdays (skips Sat/Sun)")

    # --- weekly hits the requested weekday ---
    wk = schedule.next_run_after({"kind": "weekly", "dow": 0, "time": "07:00"}, NOW)
    assert wk.weekday() == 0 and wk.hour == 7 and wk > NOW, wk
    print("PASS next_run_after weekly")

    # --- interval + once ---
    iv = schedule.next_run_after({"kind": "interval", "interval_min": 45}, NOW)
    assert iv == NOW + timedelta(minutes=45), iv
    future = (NOW + timedelta(hours=1)).timestamp()
    assert schedule.next_run_after({"kind": "once", "at": future}, NOW) is not None
    past = (NOW - timedelta(hours=1)).timestamp()
    assert schedule.next_run_after({"kind": "once", "at": past}, NOW) is None
    print("PASS next_run_after interval + once (spent once -> None)")

    # --- add/remove/persist against a temp schedules.json ---
    real_path = config.SCHEDULE_PATH
    with tempfile.TemporaryDirectory() as td:
        config.SCHEDULE_PATH = Path(td) / "schedules.json"
        schedule._jobs = []
        job = schedule.add_job("brief my inbox", "daily 08:00", now=NOW)
        assert job["id"] == "s1" and job["next_run"] > NOW.timestamp()
        assert config.SCHEDULE_PATH.exists(), "add_job should persist"

        # reload from disk -> same job survives a restart
        schedule._jobs = []
        loaded = schedule.load()
        assert len(loaded) == 1 and loaded[0]["text"] == "brief my inbox"
        print("PASS add_job persists + load() round-trips")

        # a past 'once' is refused
        try:
            schedule.add_job("late", "once 2000-01-01 00:00", now=NOW)
            assert False, "past once should raise"
        except ValueError:
            pass
        print("PASS add_job rejects a past 'once'")

        # --- fire_due: recurring job fires, advances, stays ---
        schedule._jobs = []
        rec = schedule.add_job("morning brief", "daily 08:00", now=NOW)
        due_at = datetime.fromtimestamp(rec["next_run"]) + timedelta(seconds=1)
        calls = []
        fired = schedule.fire_due(lambda t, jid: calls.append((t, jid)), now=due_at)
        assert fired == [rec["id"]], fired
        assert calls == [("morning brief", rec["id"])], calls
        assert schedule.jobs() and schedule.jobs()[0]["next_run"] > due_at.timestamp()
        print("PASS fire_due recurring (enqueued, rolled forward)")

        # --- fire_due: 'once' fires then is removed ---
        schedule._jobs = []
        onej = schedule.add_job("one shot", f"once {(NOW + timedelta(hours=2)).strftime('%Y-%m-%d %H:%M')}", now=NOW)
        after = datetime.fromtimestamp(onej["next_run"]) + timedelta(seconds=1)
        calls2 = []
        schedule.fire_due(lambda t, jid: calls2.append(jid), now=after)
        assert calls2 == [onej["id"]] and schedule.jobs() == [], schedule.jobs()
        print("PASS fire_due once (fires then removed)")

        # --- remove_job ---
        schedule._jobs = []
        j = schedule.add_job("x", "hourly", now=NOW)
        assert schedule.remove_job(j["id"]) is True and schedule.jobs() == []
        assert schedule.remove_job("nope") is False
        print("PASS remove_job")

    config.SCHEDULE_PATH = real_path

    # --- describe ---
    assert schedule.describe({"kind": "weekdays", "time": "08:00"}) == "weekdays at 08:00"
    assert schedule.describe({"kind": "interval", "interval_min": 120}) == "every 2h"
    assert schedule.describe({"kind": "interval", "interval_min": 45}) == "every 45m"
    print("PASS describe")

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    main()
