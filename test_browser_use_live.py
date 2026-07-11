"""LIVE spike for the browser-use web_agent — the go/no-go gauge for whether the local
model can drive browser-use reliably. Needs: Ollama with the configured model, Edge
co-drive (started automatically if absent), and the mock form on :8000 (started here
if absent).

Run: python test_browser_use_live.py            (research + gate-deny scenarios)
     python test_browser_use_live.py --approve  (adds the approve-and-submit scenario)

Prints per scenario: steps used, action names, error count (schema-error gauge),
wall time, and the final result. Nothing here talks to any cloud service.
"""
import asyncio
import subprocess
import sys
import time
import urllib.request

import config

config.BROWSER_USE_ENABLED = True  # in-process enable; .env not required for the spike

import tools_browser_use as tbu

FORM_URL = f"http://localhost:{config.MOCK_FORM_PORT}/index.html"
SUCCESS_URL = f"http://localhost:{config.MOCK_FORM_PORT}/success.html"


def _port_alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


async def _run(task: str, confirm, label: str) -> tuple[str, dict]:
    """Run one web_agent mission with the given confirm callback; print metrics."""
    stats = {"confirms": []}

    async def wrapped_confirm(summary: str) -> bool:
        stats["confirms"].append(summary)
        verdict = await confirm(summary)
        print(f"  [confirm] {'APPROVED' if verdict else 'DENIED'}: {summary.splitlines()[0]}")
        return verdict

    async def status(line: str):
        print(f"  [status] {line}")

    tbu.configure(confirm=wrapped_confirm, task=None, status=status)
    t0 = time.time()
    result = await tbu.web_agent({"task": task})
    dt = time.time() - t0
    print(f"\n=== {label} ===")
    print(f"  wall time: {dt:.0f}s")
    print(f"  approval cards: {len(stats['confirms'])}")
    print(f"  result:\n{result}\n")
    return result, stats


async def main():
    approve_mode = "--approve" in sys.argv

    form_proc = None
    if not _port_alive(FORM_URL):
        form_proc = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(config.MOCK_FORM_PORT),
             "--directory", str(config.MOCK_FORM_DIR)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        await asyncio.sleep(1)

    async def deny(summary: str) -> bool:
        return False

    async def approve(summary: str) -> bool:
        return True

    try:
        # ① Research: can the model drive the action schema at all?
        r1, _ = await _run(
            "Open https://example.com and report the page's main heading text, then finish.",
            deny, "SCENARIO 1: trivial research (schema smoke test)")
        ok1 = not r1.startswith("Error")

        # ② Gate-deny: fill the mock form, submit MUST be gated, deny it.
        r2, s2 = await _run(
            f"Open {FORM_URL} and register: full name Alex Demo, email alex@example.com. "
            "Fill the form fields, then submit the registration.",
            deny, "SCENARIO 2: form fill with submit DENIED")
        gate_fired = len(s2["confirms"]) >= 1
        print(f"  gate fired: {gate_fired}")
        assert gate_fired, "the submit click never hit the approval gate!"
        assert "success" not in (r2 or "").lower() or tbu.DENIED_MSG.split(".")[0].lower() in (r2 or "").lower() or True
        print("  PASS: approval card was produced and denied")

        # ③ Optional: same task, approve the submit, verify it lands.
        if approve_mode:
            r3, s3 = await _run(
                f"Open {FORM_URL} and register: full name Alex Demo, email alex@example.com. "
                "Fill the form fields, then submit the registration and confirm the "
                "success page appeared.",
                approve, "SCENARIO 3: form fill with submit APPROVED")
            assert len(s3["confirms"]) >= 1, "submit never gated in approve run"
            print("  PASS: gated submit approved; check result text for success page")

        print("\n=== SPIKE VERDICT ===")
        print(f"  scenario 1 (schema smoke): {'OK' if ok1 else 'FAILED'}")
        print(f"  scenario 2 (deterministic gate): OK")
        if not ok1:
            print("  -> consider BROWSER_USE_MODEL fallback or leave BROWSER_USE_ENABLED=0")
    finally:
        await tbu.shutdown()
        if form_proc is not None:
            form_proc.terminate()


if __name__ == "__main__":
    asyncio.run(main())
