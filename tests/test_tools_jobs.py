import config
from browser_agent import ApplyResult
from tools_jobs import apply_to_job

URL = "https://example.com/apply/123"


def _configure_applicant(tmp_path, monkeypatch):
    profile_path = tmp_path / "profile.yaml"
    profile_path.write_text(
        "applicant:\n  first_name: Alex\n  last_name: Kim\n  email: alex@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path)
    import tools_jobs
    monkeypatch.setattr(tools_jobs, "load_profile", lambda: config.load_profile(profile_path))


async def test_missing_job_store_or_gate_is_an_error(tool_ctx):
    tool_ctx.job_store = None
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("ERROR") and "not wired up" in out


async def test_already_applied_is_skipped(tool_ctx, tmp_path, monkeypatch):
    _configure_applicant(tmp_path, monkeypatch)
    tool_ctx.job_store.mark_applied(URL, notes="done")
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("SKIPPED") and "already applied" in out


async def test_in_progress_is_skipped(tool_ctx, tmp_path, monkeypatch):
    _configure_applicant(tmp_path, monkeypatch)
    tool_ctx.job_store.mark_in_progress(URL)
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("SKIPPED") and "in progress" in out


async def test_no_applicant_profile_is_an_error(tool_ctx):
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("ERROR") and "applicant profile" in out


async def test_submitted_marks_job_store_applied(tool_ctx, tmp_path, monkeypatch):
    _configure_applicant(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "browser_agent.run_apply_agent",
        lambda *a, **kw: _fake_result("submitted", "Application submitted."),
    )
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("Applied to")
    assert tool_ctx.job_store.has_applied(URL)


async def test_denied_marks_job_store_denied(tool_ctx, tmp_path, monkeypatch):
    _configure_applicant(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "browser_agent.run_apply_agent",
        lambda *a, **kw: _fake_result("denied", "DENIED by owner"),
    )
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("DENIED")
    assert not tool_ctx.job_store.has_applied(URL)
    assert tool_ctx.job_store.get(URL).status == "denied"


async def test_dry_run_does_not_mark_applied(tool_ctx, tmp_path, monkeypatch):
    _configure_applicant(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "browser_agent.run_apply_agent",
        lambda *a, **kw: _fake_result("dry_run", "DRY RUN: ready"),
    )
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("DRY RUN")
    assert not tool_ctx.job_store.has_applied(URL)
    # dry runs don't finalize a status — the earlier mark_in_progress record is left
    # as-is and self-expires (stale_after_s), so a dry run never blocks a real retry
    assert tool_ctx.job_store.get(URL).status == "in_progress"


async def test_failed_marks_job_store_failed(tool_ctx, tmp_path, monkeypatch):
    _configure_applicant(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "browser_agent.run_apply_agent",
        lambda *a, **kw: _fake_result("failed", "selector not found"),
    )
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("FAILED")
    assert tool_ctx.job_store.get(URL).status == "failed"


async def test_exception_marks_job_store_failed_and_never_raises(tool_ctx, tmp_path, monkeypatch):
    _configure_applicant(tmp_path, monkeypatch)

    async def _boom(*a, **kw):
        raise RuntimeError("browser crashed")

    monkeypatch.setattr("browser_agent.run_apply_agent", _boom)
    out = await apply_to_job(tool_ctx, URL)
    assert out.startswith("ERROR: RuntimeError: browser crashed")
    assert tool_ctx.job_store.get(URL).status == "failed"


async def _fake_result(status: str, summary: str) -> ApplyResult:
    return ApplyResult(status=status, summary=summary)
