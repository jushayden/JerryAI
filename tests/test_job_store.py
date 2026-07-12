import json

from job_store import JobStore, normalize_url


def test_normalize_url_strips_tracking_params_and_fragment():
    a = normalize_url("https://Example.com/apply/123?utm_source=Indeed&utm_campaign=x#top")
    b = normalize_url("https://example.com/apply/123")
    assert a == b


def test_normalize_url_keeps_real_query_params():
    a = normalize_url("https://example.com/apply?step=1&stepname=personalInformation")
    b = normalize_url("https://example.com/apply?step=2&stepname=personalInformation")
    assert a != b


def test_normalize_url_trailing_slash(tmp_path):
    assert normalize_url("https://example.com/apply/") == normalize_url("https://example.com/apply")


def test_roundtrip_applied(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    url = "https://example.com/apply/123"
    assert store.get(url) is None
    assert not store.has_applied(url)

    store.mark_in_progress(url)
    assert store.is_in_progress(url)
    assert not store.has_applied(url)

    store.mark_applied(url, notes="submitted after approval")
    rec = store.get(url)
    assert rec.status == "applied"
    assert rec.applied_at is not None
    assert store.has_applied(url)
    assert not store.is_in_progress(url)


def test_mark_failed_and_denied(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    url = "https://example.com/apply/456"
    store.mark_failed(url, notes="selector not found")
    assert store.get(url).status == "failed"
    assert not store.has_applied(url)

    store.mark_denied(url)
    assert store.get(url).status == "denied"
    assert not store.has_applied(url)


def test_corrupt_file_recovers_to_empty(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text("{not valid json", encoding="utf-8")
    store = JobStore(path)
    assert store.get("https://example.com/x") is None
    # writing after a corrupt read should heal the file
    store.mark_in_progress("https://example.com/x")
    assert json.loads(path.read_text(encoding="utf-8"))


def test_missing_file_is_empty(tmp_path):
    store = JobStore(tmp_path / "does_not_exist.json")
    assert store.get("https://example.com/x") is None
    assert not store.has_applied("https://example.com/x")


def test_stale_in_progress_is_retryable(tmp_path):
    class Clock:
        now = 1000.0
        def __call__(self):
            return self.now

    clock = Clock()
    store = JobStore(tmp_path / "jobs.json", clock=clock, stale_after_s=100.0)
    url = "https://example.com/apply/789"
    store.mark_in_progress(url)
    assert store.is_in_progress(url)

    clock.now += 101.0  # advance past stale_after_s
    assert not store.is_in_progress(url)


def test_no_leftover_tmp_file_after_write(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    store.mark_applied("https://example.com/x")
    assert not (tmp_path / "jobs.json.tmp").exists()


def test_atomic_write_survives_concurrent_style_updates(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    for i in range(5):
        store.mark_in_progress(f"https://example.com/apply/{i}")
    data = json.loads((tmp_path / "jobs.json").read_text(encoding="utf-8"))
    assert len(data) == 5
