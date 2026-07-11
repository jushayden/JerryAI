"""Playwright browser tools.

Uses the SYNC Playwright API pinned to one dedicated worker thread:
- async Playwright is fragile inside a long-lived Telegram app on Windows
  (event-loop policy / subprocess issues)
- sync Playwright objects have greenlet thread affinity, so they must only
  ever be touched from the SAME thread — hence a max_workers=1 executor,
  never asyncio.to_thread.

DOM-breakage policy: when a selector fails, screenshot the page, send it to
the owner, and fail loudly. Never guess.
"""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from agent import ToolSpec
from notify import ToolContext

BROWSER_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pw")

NAV_TIMEOUT_MS = 20_000
SELECTOR_TIMEOUT_MS = 10_000
MAX_EXTRACT_CHARS = 8_000


async def run_in_browser_thread(fn, /, *args, **kwargs):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        BROWSER_EXECUTOR, functools.partial(fn, *args, **kwargs))


class SelectorError(Exception):
    def __init__(self, selector: str, screenshot: Path | None):
        super().__init__(f"selector not found: {selector}")
        self.selector = selector
        self.screenshot = screenshot


class BrowserSession:
    """Sync Playwright session. Only ever call methods from the pw thread."""

    def __init__(self, screenshots_dir: Path):
        self.screenshots_dir = screenshots_dir
        self._pw = None
        self._context = None
        self._page = None

    def _assert_thread(self) -> None:
        assert threading.current_thread().name.startswith("pw"), \
            "BrowserSession must only be used from the browser thread"

    def start(self, headless: bool = True, user_data_dir: str | Path | None = None) -> None:
        self._assert_thread()
        if self._page is not None:
            return
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        if user_data_dir:
            self._context = self._pw.chromium.launch_persistent_context(
                str(user_data_dir), headless=headless,
                viewport={"width": 1280, "height": 800})
            self._page = self._context.pages[0] if self._context.pages \
                else self._context.new_page()
        else:
            browser = self._pw.chromium.launch(headless=headless)
            self._context = browser.new_context(
                viewport={"width": 1280, "height": 800})
            self._page = self._context.new_page()

    @property
    def page(self):
        self._assert_thread()
        if self._page is None:
            raise RuntimeError("browser session not started")
        return self._page

    def goto(self, url: str, timeout_ms: int = NAV_TIMEOUT_MS) -> str:
        page = self.page
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        return f"{page.title()} — {page.url}"

    def extract_text(self, selector: str = "body",
                     timeout_ms: int = SELECTOR_TIMEOUT_MS) -> str:
        page = self.page
        self.wait_for(selector, timeout_ms)
        text = page.locator(selector).first.inner_text(timeout=timeout_ms)
        return text[:MAX_EXTRACT_CHARS]

    def screenshot(self, hint: str = "page") -> Path:
        page = self.page
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)
        safe_hint = "".join(c if c.isalnum() else "_" for c in hint)[:40]
        path = self.screenshots_dir / f"{int(time.time())}_{safe_hint}.png"
        page.screenshot(path=str(path))
        return path

    def wait_for(self, selector: str, timeout_ms: int = SELECTOR_TIMEOUT_MS) -> None:
        from playwright.sync_api import TimeoutError as PWTimeout
        try:
            self.page.wait_for_selector(selector, timeout=timeout_ms)
        except PWTimeout:
            shot = None
            try:
                shot = self.screenshot(f"selector_fail_{selector}")
            except Exception:
                pass
            raise SelectorError(selector, shot) from None

    def fill(self, selector: str, value: str) -> None:
        self.wait_for(selector)
        self.page.fill(selector, value)

    def click(self, selector: str) -> None:
        self.wait_for(selector)
        self.page.click(selector)

    def close(self) -> None:
        self._assert_thread()
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._context = self._page = None


_scratch: BrowserSession | None = None


def get_scratch_session(cfg) -> BrowserSession:
    """Headless throwaway session for generic browsing. pw-thread only."""
    global _scratch
    if _scratch is None:
        _scratch = BrowserSession(cfg.downloads_dir / "screens")
        _scratch.start(headless=True)
    return _scratch


async def _report_selector_error(ctx: ToolContext, e: SelectorError) -> str:
    if e.screenshot is not None:
        await ctx.notifier.send_photos([e.screenshot],
                                       caption=f"Selector failed: {e.selector}")
        return (f"ERROR: selector '{e.selector}' not found — the page layout may "
                f"have changed. Screenshot sent to the owner.")
    return f"ERROR: selector '{e.selector}' not found and screenshot failed."


async def browser_goto(ctx: ToolContext, url: str) -> str:
    def work():
        return get_scratch_session(ctx.cfg).goto(url)
    return await run_in_browser_thread(work)


async def browser_extract(ctx: ToolContext, url: str, selector: str = "body") -> str:
    def work():
        session = get_scratch_session(ctx.cfg)
        session.goto(url)
        return session.extract_text(selector)
    try:
        return await run_in_browser_thread(work)
    except SelectorError as e:
        return await _report_selector_error(ctx, e)


async def browser_screenshot(ctx: ToolContext, url: str) -> str:
    def work():
        session = get_scratch_session(ctx.cfg)
        session.goto(url)
        return session.screenshot(url.split("//")[-1][:30])
    path = await run_in_browser_thread(work)
    await ctx.notifier.send_photos([path], caption=url)
    return f"Screenshot of {url} sent to owner ({path})"


async def browser_fill_and_submit(ctx: ToolContext, url: str, fields: dict,
                                  submit_selector: str) -> str:
    """GATED — fills a form and clicks submit."""
    def work():
        session = get_scratch_session(ctx.cfg)
        session.goto(url)
        for selector, value in dict(fields).items():
            session.fill(selector, str(value))
        session.click(submit_selector)
        session.page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
        shot = session.screenshot("after_submit")
        return session.page.url, shot
    try:
        final_url, shot = await run_in_browser_thread(work)
    except SelectorError as e:
        return await _report_selector_error(ctx, e)
    await ctx.notifier.send_photos([shot], caption=f"Form submitted — now at {final_url}")
    return f"Form submitted. Landed on {final_url}. Proof screenshot sent."


def _schema(name: str, description: str, params: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": params, "required": required},
        },
    }


def register(registry) -> None:
    registry.register(ToolSpec(
        name="browser_extract",
        func=browser_extract,
        schema=_schema("browser_extract",
                       "Open a web page and extract its visible text.",
                       {"url": {"type": "string", "description": "Full URL to open"},
                        "selector": {"type": "string",
                                     "description": "CSS selector, default 'body'"}},
                       ["url"]),
    ))
    registry.register(ToolSpec(
        name="browser_screenshot",
        func=browser_screenshot,
        schema=_schema("browser_screenshot",
                       "Open a web page and send a screenshot of it to the owner.",
                       {"url": {"type": "string", "description": "Full URL to open"}},
                       ["url"]),
    ))
    registry.register(ToolSpec(
        name="browser_fill_and_submit",
        func=browser_fill_and_submit,
        gated=True,
        schema=_schema("browser_fill_and_submit",
                       "Fill a web form and submit it. Requires owner approval.",
                       {"url": {"type": "string"},
                        "fields": {"type": "object",
                                   "description": "CSS selector -> value to type"},
                        "submit_selector": {"type": "string",
                                            "description": "CSS selector of the submit button"}},
                       ["url", "fields", "submit_selector"]),
    ))
