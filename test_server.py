"""Offline tests for the loopback extension API in server.py. Run: python test_server.py

Covers pairing-token auth, extension-only CORS, request validation, queue limits,
and secret redaction without Telegram, Ollama, or a real browser.
"""
import asyncio
import tempfile
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

import config

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


async def main():
    original = (config.LOCAL_TOKEN, config.ALLOWED_CHAT_ID, config.EVENTS_LOG, config.MAX_QUEUED_TASKS)
    with tempfile.TemporaryDirectory(prefix="tora_server_test_") as tmp:
        config.EVENTS_LOG = Path(tmp) / "events.jsonl"
        config.LOCAL_TOKEN = "test-pairing-key-" + "x" * 32
        config.ALLOWED_CHAT_ID = 0
        import server
        import state

        created = []

        async def on_created(task):
            created.append(task.id)

        server.configure(task_created_cb=on_created)
        ok = {"X-Pocket-Token": config.LOCAL_TOKEN, "Origin": EXT}
        try:
            async with TestClient(TestServer(server.create_app())) as client:
                # --- pairing token is required, compared exactly, and web origins are refused ---
                assert (await client.get("/health")).status == 403
                assert (await client.get("/health", headers={"X-Pocket-Token": "wrong"})).status == 403
                assert (await client.get("/health", headers={"X-Pocket-Token": config.LOCAL_TOKEN[:-1]})).status == 403
                assert (await client.get("/health", headers={
                    "X-Pocket-Token": config.LOCAL_TOKEN, "Origin": "https://evil.example"})).status == 403
                # a request without an Origin header (same-origin tools, curl) is fine with the token
                r = await client.get("/health", headers={"X-Pocket-Token": config.LOCAL_TOKEN})
                assert r.status == 200 and await r.json() == {"status": "ok", "telegram_paired": False}
                print("PASS token auth and origin checks")

                # --- CORS preflight: only the extension origin gets an allow header ---
                assert (await client.options("/task", headers={"Origin": "https://evil.example"})).status == 403
                r = await client.options("/task", headers={"Origin": EXT})
                assert r.status == 204 and r.headers["Access-Control-Allow-Origin"] == EXT
                assert "X-Pocket-Token" in r.headers["Access-Control-Allow-Headers"]
                print("PASS extension-only preflight")

                # --- unpaired Telegram: tasks are refused with guidance ---
                r = await client.post("/task", json={"text": "hi", "url": "https://example.test/"}, headers=ok)
                assert r.status == 503 and "Pair your Telegram" in (await r.json())["error"], await r.text()
                print("PASS unpaired bot refuses tasks")

                config.ALLOWED_CHAT_ID = 4242
                r = await client.get("/health", headers=ok)
                assert (await r.json())["telegram_paired"] is True

                # --- request validation ---
                bad = [
                    ("not json", {"Content-Type": "application/json"}),
                    (b"[1, 2]", {"Content-Type": "application/json"}),
                ]
                for body, extra in bad:
                    r = await client.post("/task", data=body, headers={**ok, **extra})
                    assert r.status == 400, (body, r.status)
                for payload in [{"text": "   "}, {"text": "x" * 4001}, {"text": 5},
                                {"text": "hi", "url": "edge://settings"},
                                {"text": "hi", "url": "javascript:alert(1)"},
                                {"text": "hi", "url": "https://user:pw@example.test/"},
                                {"text": "hi", "url": 7}]:
                    r = await client.post("/task", json=payload, headers=ok)
                    assert r.status == 400, (payload, r.status)
                r = await client.post("/task", data=b'{"text": "' + b"a" * 20000 + b'"}',
                                      headers={**ok, "Content-Type": "application/json"})
                assert r.status == 413, r.status
                assert created == [] and state.queue.qsize() == 0
                print("PASS invalid requests rejected without queuing")

                # --- a valid badge task is queued, announced, and readable ---
                r = await client.post("/task", json={"text": "  summarize this  ", "url": "https://example.test/page#top"},
                                      headers=ok)
                assert r.status == 201, await r.text()
                tid = (await r.json())["id"]
                assert created == [tid]
                task = next(t for t in state.tasks if t.id == tid)
                assert task.text == "summarize this" and task.origin == "badge"
                assert task.source_url == "https://example.test/page#top" and task.status == "queued"
                r = await client.get(f"/task/{tid}", headers=ok)
                body = await r.json()
                assert r.status == 200 and body["status"] == "queued" and body["origin"] == "badge"
                assert body["steps"] == 0 and body["artifacts"] == 0
                assert (await client.get(f"/task/{tid}")).status == 403
                assert (await client.get("/task/000000", headers=ok)).status == 404
                # a badge task without a page URL is still accepted
                r = await client.post("/task", json={"text": "open notepad"}, headers=ok)
                assert r.status == 201 and state.tasks[-1].source_url is None
                print("PASS valid tasks queued and reported")

                # --- queue cap ---
                config.MAX_QUEUED_TASKS = state.queue.qsize()
                r = await client.post("/task", json={"text": "one more"}, headers=ok)
                assert r.status == 429
                print("PASS queue limit")

                # --- live secrets never leak through the status endpoint ---
                state.register_secret("hunter2-secret")
                task.result = "logged in with hunter2-secret"
                task.needs = "otp hunter2-secret"
                state.add_step(task, "typed hunter2-secret")
                body = await (await client.get(f"/task/{tid}", headers=ok)).json()
                for field in ("result", "needs", "step"):
                    assert "hunter2-secret" not in body[field] and "[REDACTED]" in body[field], body
                state.forget_secret("hunter2-secret")
                print("PASS secrets redacted in task status")
        finally:
            state.tasks.clear()
            while not state.queue.empty():
                state.queue.get_nowait()
            config.LOCAL_TOKEN, config.ALLOWED_CHAT_ID, config.EVENTS_LOG, config.MAX_QUEUED_TASKS = original
    print("test_server.py: all assertions passed")


if __name__ == "__main__":
    asyncio.run(main())
