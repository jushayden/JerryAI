"""Offline tests for tools_email.py — no network, no credentials needed.

Stubs the blocking Gmail helpers (_send_sync/_find_sync/...) so we can prove the
approval gating and argument handling without a live account. Run: python test_tools_email.py
"""
import asyncio
import base64
import inspect

import tools_email


async def deny(summary):
    return False


async def allow(summary):
    return True


async def boom(summary):
    raise AssertionError("confirm_cb must not be called here")


async def main():
    calls = {}

    # --- stub every blocking Gmail call so nothing hits the network ---
    def fake_send(to, s, b, cc, bcc):
        calls["send"] = (to, s, b)
        return "SENT"

    def fake_reply(match, body):
        calls["reply"] = (match["from"], body)
        return "REPLIED"

    def fake_draft(to, s, b, cc, bcc):
        calls["draft"] = to
        return "DRAFTED"

    def fake_trash(mid):
        calls["trash"] = mid
        return "TRASHED"

    tools_email._send_sync = fake_send
    tools_email._reply_sync = fake_reply
    tools_email._draft_sync = fake_draft
    tools_email._trash_sync = fake_trash
    fake_match = {"id": "m1", "threadId": "t1", "from": "alice@x.com",
                  "subject": "Invoice", "message_id": "<abc@x>", "references": "", "snippet": "hi"}
    tools_email._find_sync = lambda q: fake_match

    # --- registry contract shape ---
    expected = {"scan_inbox", "send_email", "reply_email", "create_draft", "trash_email"}
    assert set(tools_email.TOOLS) == expected, set(tools_email.TOOLS)
    for name, entry in tools_email.TOOLS.items():
        assert set(entry) >= {"schema", "fn"}, name
        fn = entry["schema"]["function"]
        assert entry["schema"]["type"] == "function"
        assert fn["name"] == name and fn["description"] and "parameters" in fn
        assert inspect.iscoroutinefunction(entry["fn"]), name
    print("PASS TOOLS registry contract shape (5 tools)")

    # --- missing required args -> graceful Error, no send ---
    assert (await tools_email.send_email({"subject": "x", "body": "y"})).startswith("Error"), "no-to"
    assert (await tools_email.reply_email({"body": "y"})).startswith("Error"), "no-query"
    assert (await tools_email.trash_email({})).startswith("Error"), "no-query trash"
    assert "send" not in calls
    print("PASS missing-arg validation (nothing sent)")

    # --- send_email: DENIED leaves it unsent ---
    tools_email.configure(confirm=deny, preauth=False)
    r = await tools_email.send_email({"to": "bob@x.com", "subject": "Hi", "body": "yo"})
    assert r.startswith("User DENIED"), r
    assert "send" not in calls, "send fired despite denial"
    print("PASS send_email denied -> not sent")

    # --- send_email: APPROVED goes through ---
    tools_email.configure(confirm=allow, preauth=False)
    r = await tools_email.send_email({"to": "bob@x.com", "subject": "Hi", "body": "yo"})
    assert r == "SENT" and calls["send"] == ("bob@x.com", "Hi", "yo"), (r, calls.get("send"))
    print("PASS send_email approved -> sent")

    # --- send_email: preauthorized skips the gate ---
    calls.clear()
    tools_email.configure(confirm=boom, preauth=True)
    r = await tools_email.send_email({"to": "c@x.com", "subject": "P", "body": "z"})
    assert r == "SENT" and "send" in calls, r
    print("PASS send_email preauthorized -> no confirm")

    # --- reply_email: threaded, gated ---
    calls.clear()
    tools_email.configure(confirm=allow, preauth=False)
    r = await tools_email.reply_email({"query": "from:alice", "body": "thanks"})
    assert r == "REPLIED" and calls["reply"] == ("alice@x.com", "thanks"), (r, calls.get("reply"))
    tools_email.configure(confirm=deny, preauth=False)
    assert (await tools_email.reply_email({"query": "from:alice", "body": "no"})).startswith("User DENIED")
    print("PASS reply_email gated (approve sends, deny blocks)")

    # --- reply_email: no match -> reported, not sent ---
    tools_email._find_sync = lambda q: None
    r = await tools_email.reply_email({"query": "from:nobody", "body": "x"})
    assert "No message matched" in r, r
    tools_email._find_sync = lambda q: fake_match
    print("PASS reply_email no-match reported")

    # --- create_draft: reversible, NEVER gated ---
    calls.clear()
    tools_email.configure(confirm=boom, preauth=False)  # boom = fail if a gate fires
    assert (await tools_email.create_draft({"to": "d@x.com", "subject": "s", "body": "b"})) == "DRAFTED"
    assert calls["draft"] == "d@x.com", calls.get("draft")
    print("PASS create_draft is ungated")

    # --- trash_email: gated (deny blocks) ---
    calls.clear()
    tools_email.configure(confirm=deny, preauth=False)
    r = await tools_email.trash_email({"query": "from:spam"})
    assert r.startswith("User DENIED") and "trash" not in calls, r
    print("PASS trash_email denied -> not trashed")

    # --- MIME builder: real RFC822 with the right headers ---
    raw = tools_email._build_raw("x@y.com", "Subj", "Body text", cc="c@y.com",
                                 headers={"In-Reply-To": "<id@y>"})
    decoded = base64.urlsafe_b64decode(raw).decode()
    for needle in ["To: x@y.com", "Subject: Subj", "Cc: c@y.com", "In-Reply-To: <id@y>", "Body text"]:
        assert needle in decoded, needle
    print("PASS _build_raw produces valid MIME with headers")

    tools_email.configure()  # reset to defaults
    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
