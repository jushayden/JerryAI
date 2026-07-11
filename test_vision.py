"""Deterministic visual fallback test; pass --real for an installed-model smoke test."""
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import config
import tools_browser


class _Mouse:
    def __init__(self):
        self.clicks = []

    async def click(self, x, y):
        self.clicks.append((x, y))


class _FakePage:
    url = "https://example.test/visual"
    frames = []

    def __init__(self):
        self.mouse = _Mouse()

    async def screenshot(self, *, path, **kwargs):
        Path(path).write_bytes(b"fake-png")

    async def evaluate(self, script, arg=None):
        if "jerry-vision-overlay" in script and "return out" in script:
            return []
        if "innerWidth" in script:
            return {"width": 1000, "height": 700}
        if "elementFromPoint" in script:
            return {"tag": "canvas", "type": "", "text": "", "aria_label": "",
                    "in_form": False, "opaque": True}
        return ""

    async def wait_for_load_state(self, *args, **kwargs):
        return None

    async def wait_for_timeout(self, *args, **kwargs):
        return None

    async def title(self):
        return "Visual fixture"


class _FakeClient:
    async def generate(self, **kwargs):
        return {"response": ""}

    async def chat(self, *, model, **kwargs):
        if model == config.VISION_MODEL:
            return {"message": {"content": json.dumps({
                "x": 420, "y": 250, "label": "map destination",
                "confidence": 0.94, "reason": "visible marker",
            })}}
        return {"message": {"content": "ready"}}


async def deterministic_test():
    original_ctx = tools_browser._ctx
    original_client = tools_browser.ollama.AsyncClient
    original_dir = config.SCREENSHOT_DIR
    original_confirm = tools_browser._confirm_cb
    page = _FakePage()
    approvals = []

    async def fake_ctx():
        return page

    async def approve(summary):
        approvals.append(summary)
        return True

    with tempfile.TemporaryDirectory(prefix="jerry_vision_test_") as tmp:
        tools_browser._ctx = fake_ctx
        tools_browser.ollama.AsyncClient = lambda **kwargs: _FakeClient()
        tools_browser._confirm_cb = approve
        tools_browser._visual_attempts = 0
        tools_browser._visual_target = None
        config.SCREENSHOT_DIR = Path(tmp)
        result = await tools_browser.visual_inspect({"target": "map destination"})
        assert not result.startswith("Error"), result
        target = json.loads(result)
        assert target["x"] == 420 and target["confidence"] == 0.94
        result = await tools_browser.visual_click({})
        assert not result.startswith("Error"), result
        assert page.mouse.clicks == [(420, 250)]
        assert len(approvals) == 1 and "no reliable DOM semantics" in approvals[0]

    tools_browser._ctx = original_ctx
    tools_browser.ollama.AsyncClient = original_client
    tools_browser._confirm_cb = original_confirm
    config.SCREENSHOT_DIR = original_dir
    print("test_vision.py: deterministic visual fallback passed")


async def real_smoke():
    models = await tools_browser.ollama.AsyncClient(host=config.OLLAMA_HOST).list()
    names = [m.model for m in models.models]
    if not any(n.split(":")[0] == config.VISION_MODEL.split(":")[0] for n in names):
        print(f"SKIP real vision smoke: pull {config.VISION_MODEL} first")
        return
    config.BROWSER_MODE = "owned"
    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(config.MOCK_FORM_PORT),
         "--directory", str(config.MOCK_FORM_DIR)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        await asyncio.sleep(1)
        tools_browser.configure(confirm=lambda summary: asyncio.sleep(0, result=True))
        await tools_browser.browser_goto(
            {"url": f"http://localhost:{config.MOCK_FORM_PORT}/browser_tools.html"})
        result = await tools_browser.visual_inspect({"target": "the Open dialog button"})
        assert not result.startswith("Error"), result
        print("test_vision.py: real local-model smoke passed", result)
    finally:
        await tools_browser.shutdown()
        server.terminate()
        server.wait(timeout=5)


if __name__ == "__main__":
    asyncio.run(real_smoke() if "--real" in sys.argv else deterministic_test())
