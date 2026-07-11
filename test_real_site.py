"""Manual real-web acceptance test for the selected Greenhouse application page."""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import config
USE_ISOLATED_EDGE = "--edge" in sys.argv
USE_CODRIVE_EDGE = "--co-drive" in sys.argv
config.BROWSER_MODE = "edge" if (USE_ISOLATED_EDGE or USE_CODRIVE_EDGE) else "owned"
if USE_ISOLATED_EDGE:
    config.CDP_PORT = 9333
    config.CDP_URL = "http://127.0.0.1:9333"

import tools_browser


URL = "https://job-boards.greenhouse.io/claudecorps/jobs/4250200009"


def find_field(label: str):
    needle = label.lower()
    for field_id, el in tools_browser._last.items():
        if el.get("tag") != "input":
            continue
        if needle in (el.get("label") or "").lower():
            return field_id
    return None


async def deny_submit(summary):
    raise AssertionError(f"Real-site smoke must never reach an approval or submit action: {summary}")


async def main():
    edge_process = None
    if USE_ISOLATED_EDGE:
        exe = tools_browser._edge_executable()
        assert exe, "Microsoft Edge executable not found"
        profile = Path("C:/tmp/jerry-real-site-edge-profile")
        profile.mkdir(parents=True, exist_ok=True)
        edge_process = subprocess.Popen(
            [exe, f"--remote-debugging-port={config.CDP_PORT}",
             f"--user-data-dir={profile}", "--no-first-run", URL],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert await tools_browser._wait_for_cdp(), "isolated Edge CDP endpoint did not start"
    tools_browser.configure(confirm=deny_submit)
    try:
        digest = await tools_browser.browser_goto({"url": URL})
        assert not digest.startswith("Error"), digest
        assert not digest.startswith("STOP:"), digest

        first = find_field("First Name")
        last = find_field("Last Name")
        email = find_field("Email")
        if not all([first, last, email]):
            for _ in range(4):
                await tools_browser.scroll_page({"amount": 1400})
                await tools_browser.extract_form_fields({})
                first = first or find_field("First Name")
                last = last or find_field("Last Name")
                email = email or find_field("Email")
                if all([first, last, email]):
                    break
        assert all([first, last, email]), (
            "Greenhouse fields were not discoverable; current controls: "
            + ", ".join((el.get("label") or el["id"]) for el in tools_browser._last.values())
        )

        await tools_browser.scroll_to_element({"field_id": first})
        await tools_browser.fill_field({"field_id": first, "value": "Jerry"})
        await tools_browser.fill_field({"field_id": last, "value": "Validation"})
        result = await tools_browser.fill_field(
            {"field_id": email, "value": "jerry-validation@example.invalid"})
        assert "jerry-validation@example.invalid" in result

        vision = await tools_browser.visual_inspect({
            "target": "the required First Name input field in the job application form"})
        assert not vision.startswith("Error"), vision
        target = json.loads(vision)
        assert target["confidence"] >= config.VISION_MIN_CONFIDENCE, target

        # Clear all synthetic values and never click Apply/Submit.
        await tools_browser.fill_field({"field_id": first, "value": ""})
        await tools_browser.fill_field({"field_id": last, "value": ""})
        await tools_browser.fill_field({"field_id": email, "value": ""})
        print("REAL SITE PASS", json.dumps(target, ensure_ascii=False))
    finally:
        if USE_ISOLATED_EDGE and tools_browser._browser is not None:
            try:
                await tools_browser._browser.close()  # only the isolated test profile
            except Exception:
                pass
        await tools_browser.shutdown()
        if edge_process is not None and edge_process.poll() is None:
            edge_process.terminate()


if __name__ == "__main__":
    asyncio.run(main())
