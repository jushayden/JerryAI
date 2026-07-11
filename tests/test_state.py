import json

import pytest

from state import CooldownStore, EventLog, SearchCache


def test_event_log_writes_valid_jsonl_with_emoji(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("task_received", task_id="t1", text="post 'Testing my AI agent 🚀' to X")
    log.append("task_done", task_id="t1")
    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["kind"] == "task_received"
    assert "🚀" in first["text"]
    assert first["ts"]


def test_event_log_rejects_unknown_kind(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    with pytest.raises(ValueError):
        log.append("made_up_kind", task_id="t1")


def test_event_log_truncates_result(tmp_path):
    log = EventLog(tmp_path / "events.jsonl")
    log.append("tool_result", task_id="t1", result="x" * 5000)
    rec = json.loads((tmp_path / "events.jsonl").read_text(encoding="utf-8"))
    assert len(rec["result"]) == 500


def test_cooldown_blocks_31st_instagram_action(tmp_path, fake_clock):
    store = CooldownStore(tmp_path / "cd.json", clock=fake_clock)
    store.configure("instagram", per_hour=30, min_delay_s=0.0)
    for _ in range(30):
        allowed, _ = store.check("instagram")
        assert allowed
        store.record("instagram")
    allowed, wait = store.check("instagram")
    assert not allowed
    assert wait > 0
    # window slides: an hour later it's allowed again
    fake_clock.advance(3601)
    allowed, _ = store.check("instagram")
    assert allowed


def test_cooldown_min_delay(tmp_path, fake_clock):
    store = CooldownStore(tmp_path / "cd.json", clock=fake_clock)
    store.configure("instagram", per_hour=30, min_delay_s=5.0)
    store.record("instagram")
    allowed, wait = store.check("instagram")
    assert not allowed and 0 < wait <= 5.0
    fake_clock.advance(5.1)
    allowed, _ = store.check("instagram")
    assert allowed


def test_cooldown_unconfigured_platform_unlimited(tmp_path, fake_clock):
    store = CooldownStore(tmp_path / "cd.json", clock=fake_clock)
    for _ in range(100):
        allowed, _ = store.check("reddit")
        assert allowed
        store.record("reddit")


def test_cooldown_persists_across_instances(tmp_path, fake_clock):
    store = CooldownStore(tmp_path / "cd.json", clock=fake_clock)
    store.configure("instagram", per_hour=1)
    store.record("instagram")
    store2 = CooldownStore(tmp_path / "cd.json", clock=fake_clock)
    store2.configure("instagram", per_hour=1)
    allowed, _ = store2.check("instagram")
    assert not allowed


def test_search_cache_ttl(fake_clock):
    cache = SearchCache(ttl_s=600, clock=fake_clock)
    key = SearchCache.key("reddit", "search", "Tesla Model 2", 10)
    assert cache.get(key) is None
    cache.put(key, [{"text": "hit"}])
    assert cache.get(key) == [{"text": "hit"}]
    fake_clock.advance(599)
    assert cache.get(key) is not None
    fake_clock.advance(2)
    assert cache.get(key) is None


def test_search_cache_key_normalizes():
    assert SearchCache.key("reddit", "search", "  TESLA ", 5) == "reddit:search:tesla:5"
