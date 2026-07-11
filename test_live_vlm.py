"""Manual 30B VLM smoke test against a complex, live Microsoft page."""
import asyncio
import json

import config
config.BROWSER_MODE = "owned"

import tools_browser


URL = "https://www.microsoft.com/en-us/edge/features"


async def main():
    async def deny(summary):
        raise AssertionError(f"Live VLM smoke is read-only: {summary}")

    tools_browser.configure(confirm=deny)
    try:
        digest = await tools_browser.browser_goto({"url": URL})
        assert not digest.startswith(("Error", "STOP:")), digest
        result = await tools_browser.visual_inspect({
            "target": "the Download Edge control in the top navigation or page header"})
        assert not result.startswith("Error"), result
        target = json.loads(result)
        assert target["confidence"] >= config.VISION_MIN_CONFIDENCE, target
        print("LIVE VLM PASS", json.dumps(target, ensure_ascii=False))
    finally:
        await tools_browser.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
