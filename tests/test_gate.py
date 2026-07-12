import json

from gate import Gate, GateDecision, build_card_text, card_html
from state import EventLog


class ScriptedApprover:
    def __init__(self, verdict):
        self.verdict = verdict
        self.requests = []

    async def request_approval(self, **kwargs):
        self.requests.append(kwargs)
        return self.verdict


def make_gate(tmp_path, verdict):
    approver = ScriptedApprover(verdict)
    log = EventLog(tmp_path / "events.jsonl")
    return Gate(approver, log, timeout_s=1), approver


async def test_reads_are_autonomous(tool_ctx, tmp_path):
    gate, approver = make_gate(tmp_path, verdict=False)
    for tool in ("social_search", "social_get_feed", "social_get_profile",
                 "fs_read_file", "fs_write_file", "browser_screenshot"):
        d = await gate.check(tool, {}, tool_ctx)
        assert d == GateDecision(True, "autonomous"), tool
    assert approver.requests == []  # owner never bothered


async def test_post_requires_approval_and_shows_exact_text(tool_ctx, tmp_path):
    gate, approver = make_gate(tmp_path, verdict=True)
    d = await gate.check("social_post", {"platform": "x", "text": "hello world 🚀"}, tool_ctx)
    assert d == GateDecision(True, "owner_approved")
    req = approver.requests[0]
    assert req["payload_text"] == "hello world 🚀"  # EXACT text, verbatim
    assert req["platform"] == "x"


async def test_denied(tool_ctx, tmp_path):
    gate, _ = make_gate(tmp_path, verdict=False)
    d = await gate.check("social_post", {"platform": "x", "text": "spam"}, tool_ctx)
    assert d == GateDecision(False, "owner_denied")


async def test_timeout_is_deny(tool_ctx, tmp_path):
    gate, _ = make_gate(tmp_path, verdict=None)
    d = await gate.check("fs_delete", {"path": "x.txt"}, tool_ctx)
    assert d == GateDecision(False, "timeout")


async def test_pre_authorized_skips_approval(tool_ctx, tmp_path):
    gate, approver = make_gate(tmp_path, verdict=False)  # would deny if asked
    tool_ctx.pre_authorized = True
    d = await gate.check("social_post", {"platform": "x", "text": "hi"}, tool_ctx)
    assert d == GateDecision(True, "pre_authorized")
    assert approver.requests == []
    events = [json.loads(l) for l in
              (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert any(e["kind"] == "gate_decision" and e["reason"] == "pre_authorized"
               for e in events)


async def test_all_decisions_logged(tool_ctx, tmp_path):
    gate, _ = make_gate(tmp_path, verdict=True)
    await gate.check("social_search", {"platform": "reddit", "query": "q"}, tool_ctx)
    await gate.check("social_post", {"platform": "x", "text": "t"}, tool_ctx)
    events = [json.loads(l) for l in
              (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["kind"] for e in events]
    assert kinds.count("gate_decision") == 2
    assert "approval_requested" in kinds and "approval_resolved" in kinds


async def test_check_custom_always_requests_approval(tool_ctx, tmp_path):
    gate, approver = make_gate(tmp_path, verdict=True)
    allowed = await gate.check_custom("apply_to_job.submit", "Job application ready to submit\nURL: https://e.com", tool_ctx)
    assert allowed is True
    assert approver.requests[0]["payload_text"] == "Job application ready to submit\nURL: https://e.com"
    assert approver.requests[0]["platform"] == "custom"


async def test_check_custom_denied(tool_ctx, tmp_path):
    gate, _ = make_gate(tmp_path, verdict=False)
    assert await gate.check_custom("apply_to_job.submit", "payload", tool_ctx) is False


async def test_check_custom_timeout_is_deny(tool_ctx, tmp_path):
    gate, _ = make_gate(tmp_path, verdict=None)
    assert await gate.check_custom("apply_to_job.submit", "payload", tool_ctx) is False


async def test_check_custom_ignores_pre_authorized(tool_ctx, tmp_path):
    """Even a '!'-prefixed task must not skip approval for a real job submission."""
    gate, approver = make_gate(tmp_path, verdict=True)
    tool_ctx.pre_authorized = True
    await gate.check_custom("apply_to_job.submit", "payload", tool_ctx)
    assert len(approver.requests) == 1  # still asked, unlike check()


async def test_check_custom_never_logs_raw_args(tool_ctx, tmp_path):
    gate, _ = make_gate(tmp_path, verdict=True)
    await gate.check_custom("apply_to_job.submit", "REDACTED payload only", tool_ctx)
    events = [json.loads(l) for l in
              (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    gate_events = [e for e in events if e["kind"] == "gate_decision"]
    assert gate_events and "args" not in gate_events[0]


async def test_check_custom_respects_explicit_timeout(tool_ctx, tmp_path):
    gate, approver = make_gate(tmp_path, verdict=True)
    await gate.check_custom("apply_to_job.submit", "payload", tool_ctx, timeout_s=42)
    assert approver.requests[0]["timeout_s"] == 42


def test_card_text_variants():
    platform, action, payload = build_card_text(
        "social_post", {"platform": "x", "text": "hi", "media_path": "p.jpg"})
    assert platform == "x" and "hi" in payload and "p.jpg" in payload
    _, action, payload = build_card_text("fs_delete", {"path": "a.txt"})
    assert "Delete" in action and "a.txt" in payload
    _, _, payload = build_card_text(
        "browser_fill_and_submit", {"url": "https://e.com", "fields": {"q": "v"}})
    assert "https://e.com" in payload and "q: v" in payload


def test_card_html_escapes():
    out = card_html("Post to X", "x", "<script>alert(1)</script>")
    assert "<script>" not in out and "&lt;script&gt;" in out
