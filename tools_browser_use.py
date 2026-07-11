"""browser-use autonomous web sub-agent — the `web_agent` tool.

Runs ALONGSIDE tools_browser (which stays the default for J-badge co-drive and simple
pages): the outer qwen3-coder loop delegates a long multi-page web mission to a bounded
browser-use Agent that drives the SAME Edge over CDP (config.CDP_URL, keep_alive — the
user's browser is never closed by us).

Safety model (deterministic, not prompt-level):
- The built-in `click` and `send_keys` actions are deleted and re-registered with the
  project's gate: gate.is_irreversible_click() on the resolved DOM node -> Telegram
  approval card via the injected confirm callback (300s timeout == deny).
- `evaluate` (arbitrary page JS could form.submit() around the gate) and browser-use's
  own file actions (write/replace/read_file, save_as_pdf — they write outside the
  tools_fs sandbox) are excluded outright.
- File uploads keep the BUILT-IN upload_file, which hard-enforces the
  available_file_paths allowlist (resume + Telegram uploads + downloads only).
- The sub-agent never receives passwords/OTPs; login walls and CAPTCHAs end the run
  with a report of what the human must do (prompt-level policy — documented limit).

Cloud kill-switches (ANONYMIZED_TELEMETRY / BROWSER_USE_CLOUD_SYNC) are set in
config.py before this module can be imported. Never use ChatBrowserUse.

Coexistence: Chromium's CDP endpoint accepts multiple clients, so Playwright
(tools_browser) and browser-use may be attached at once; the serial task queue means
they never drive simultaneously. tools_browser._ctx() re-selects its tab afterwards.
"""
import asyncio
import re
import shutil
import time
from pathlib import Path

import ollama
import yaml

import config
import gate
import state
import tools_browser

DENIED_MSG = tools_browser.DENIED_MSG

_ENTER_RE = re.compile(r"\b(enter|return)\b", re.I)

# --- per-task state (mirrors tools_browser.configure) ---
_confirm_cb = tools_browser._default_confirm
_status_cb = None            # optional: streams sub-agent step lines to the phone card
_task: "state.TaskRecord | None" = None
_session = None              # live BrowserSession during a run
_last_screenshot: str | None = None


def configure(confirm=None, task=None, secret_resolver=None, status=None):
    """Inject per-task phone callbacks and audit context (secret_resolver unused in v1:
    the sub-agent never handles credentials)."""
    global _confirm_cb, _task, _status_cb, _last_screenshot
    _confirm_cb = confirm if confirm is not None else tools_browser._default_confirm
    _task = task
    _status_cb = status
    _last_screenshot = None


def _audit(action: str, details=None, *, ok: bool | None = None) -> None:
    if _task is not None:
        state.audit_event(_task, "browser_use", action, details, ok=ok)


def _remember_source(url) -> None:
    if _task is not None and isinstance(url, str) and url.startswith(("http://", "https://")):
        state.add_source(_task, url)


def _remember_artifact(path) -> None:
    if _task is not None:
        state.add_artifact(_task, path)


# --- gate plumbing (pure, unit-testable on duck-typed nodes) ---

def _node_in_form(node) -> bool:
    """Walk parents; True if any ancestor is a <form>."""
    seen = 0
    cur = getattr(node, "parent_node", None)
    while cur is not None and seen < 50:
        try:
            if (getattr(cur, "tag_name", "") or "").lower() == "form":
                return True
        except Exception:
            break
        cur = getattr(cur, "parent_node", None)
        seen += 1
    return False


def _element_dict(node) -> dict:
    """EnhancedDOMTreeNode -> the element shape gate.is_irreversible_click expects."""
    attrs = getattr(node, "attributes", None) or {}
    text = ""
    try:
        text = (node.get_all_children_text(max_depth=2) or "").strip()
    except Exception:
        pass
    if not text:
        try:
            text = (node.get_meaningful_text_for_llm() or "").strip()
        except Exception:
            text = ""
    aria = attrs.get("aria-label", "") or ""
    if not aria:
        ax = getattr(node, "ax_node", None)
        aria = (getattr(ax, "name", "") or "") if ax is not None else ""
    tag = ""
    try:
        tag = (getattr(node, "tag_name", "") or "").lower()
    except Exception:
        pass
    return {
        "tag": tag,
        "type": (attrs.get("type", "") or "").lower(),
        "text": text[:120],
        "aria_label": aria[:120],
        "in_form": _node_in_form(node),
    }


async def _page_context(browser_session) -> str:
    """Best-effort 'title — url' for the approval card. Never raises."""
    url = title = ""
    try:
        url = await browser_session.get_current_page_url() or ""
    except Exception:
        pass
    try:
        title = await browser_session.get_current_page_title() or ""
    except Exception:
        pass
    return f"{title} — {url}".strip(" —") or "this page"


async def _dispatch(browser_session, event) -> object:
    """The exact built-in event idiom: dispatch, await, surface handler errors."""
    ev = browser_session.event_bus.dispatch(event)
    await ev
    return await ev.event_result(raise_if_any=True, raise_if_none=False)


async def _dispatch_click(browser_session, node) -> object:
    """Seam: build + dispatch the real ClickElementEvent (patched in offline tests)."""
    from browser_use.browser.events import ClickElementEvent
    return await _dispatch(browser_session, ClickElementEvent(node=node))


async def _dispatch_keys(browser_session, keys: str) -> object:
    """Seam: build + dispatch the real SendKeysEvent (patched in offline tests)."""
    from browser_use.browser.events import SendKeysEvent
    return await _dispatch(browser_session, SendKeysEvent(keys=keys))


async def _gated_click_impl(index: int, browser_session):
    """click replacement: identical to the built-in, with the gate inserted before dispatch.
    Denials return extracted_content (NOT error=) so they don't burn max_failures."""
    from browser_use import ActionResult

    node = await browser_session.get_element_by_index(index)
    if node is None:
        return ActionResult(
            extracted_content=f"Element index {index} not available - page may have "
                              "changed. Try refreshing browser state.")
    el = _element_dict(node)
    label = el["text"] or el["aria_label"] or el["tag"] or f"element {index}"
    if gate.is_irreversible_click(el):
        el["page"] = await _page_context(browser_session)
        summary = gate.summarize_submission(el, [])
        _audit("click_gate", {"label": label, "page": el["page"]})
        approved = await _confirm_cb(summary)
        if not approved:
            _audit("click_denied", {"label": label}, ok=False)
            return ActionResult(
                extracted_content=DENIED_MSG,
                long_term_memory=f"The human DENIED clicking '{label}'. Never retry it; "
                                 "adjust the approach or finish with done.")
    try:
        meta = await _dispatch_click(browser_session, node)
    except Exception as e:
        return ActionResult(error=f"Failed to click element {index}: {e}")
    if isinstance(meta, dict) and "validation_error" in meta:
        return ActionResult(error=str(meta["validation_error"]))
    _audit("click", {"label": label}, ok=True)
    return ActionResult(extracted_content=f"Clicked {label}")


async def _focus_in_form(cdp_client) -> bool:
    """Is the focused element inside a <form>? Failure -> True (over-gate bias)."""
    try:
        res = await cdp_client.send.Runtime.evaluate(
            params={"expression":
                    "!!(document.activeElement && document.activeElement.closest('form'))",
                    "returnByValue": True})
        return bool(res.get("result", {}).get("value", True))
    except Exception:
        return True


async def _gated_send_keys_impl(keys: str, browser_session, cdp_client):
    """send_keys replacement: Enter may submit the focused form, so it gates."""
    from browser_use import ActionResult

    if _ENTER_RE.search(keys or "") and await _focus_in_form(cdp_client):
        page = await _page_context(browser_session)
        summary = (f'High-impact action: press "{keys}" (may submit a form)\n'
                   f"Page/domain: {page}")
        _audit("enter_gate", {"keys": keys, "page": page})
        if not await _confirm_cb(summary):
            _audit("enter_denied", {"keys": keys}, ok=False)
            return ActionResult(
                extracted_content=DENIED_MSG,
                long_term_memory=f"The human DENIED pressing '{keys}'. Never retry it.")
    try:
        await _dispatch_keys(browser_session, keys)
    except Exception as e:
        return ActionResult(error=f"Failed to send keys: {e}")
    _audit("send_keys", {"keys": keys}, ok=True)
    return ActionResult(extracted_content=f"Sent keys: {keys}")


async def _look_at_page_impl(question: str, browser_session):
    """qwen3-vl visual inspection: colors, layout, imagery — anything the DOM digest
    can't convey (e.g. car-configurator swatches). Swaps models in VRAM like
    tools_browser.visual_inspect, then swaps the coder model back."""
    from browser_use import ActionResult

    if not config.VISION_ENABLED:
        return ActionResult(error="local vision is disabled (VISION_ENABLED=0).")
    q = (question or "").strip() or "Describe what is visible on this page."
    path = None
    try:
        png = await browser_session.take_screenshot(full_page=False)
        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.SCREENSHOT_DIR / f"web_agent_vision_{int(time.time() * 1000)}.png"
        path.write_bytes(png)
        client = ollama.AsyncClient(host=config.OLLAMA_HOST)
        await tools_browser._unload_model(client, config.BROWSER_USE_MODEL)
        try:
            response = await client.chat(
                model=config.VISION_MODEL,
                messages=[{"role": "user",
                           "content": "Answer concisely from this browser screenshot. "
                                      + q,
                           "images": [str(path)]}],
                think=False,
                options={"temperature": 0, "num_predict": 700, "num_ctx": config.NUM_CTX},
                keep_alive=0,
            )
            answer = (response["message"]["content"] or "").strip()
        finally:
            await tools_browser._unload_model(client, config.VISION_MODEL)
            try:  # pull the driving model back into VRAM
                await client.chat(model=config.BROWSER_USE_MODEL,
                                  messages=[{"role": "user", "content": "Say ready"}],
                                  options={"num_ctx": config.NUM_CTX},
                                  keep_alive=config.KEEP_ALIVE)
            except Exception:
                pass
        _audit("look_at_page", {"question": q[:120]}, ok=True)
        return ActionResult(extracted_content=answer[:2000] or "(the vision model saw nothing)")
    except Exception as e:
        return ActionResult(error=f"visual inspection failed: {e}")
    finally:
        if path is not None:
            Path(path).unlink(missing_ok=True)


# switch/close are excluded so the sub-agent physically cannot jump to (or close) the
# user's own tabs: it works only in the tab it starts in and tabs it opens itself —
# browser-use auto-follows its own new tabs, so no switch action is needed for that.
EXCLUDED_ACTIONS = ["evaluate", "write_file", "replace_file", "read_file", "save_as_pdf",
                    "switch", "close"]
REPLACED_ACTIONS = ["click", "send_keys"]


def _build_tools():
    """Tools registry with the gate built in. Built-ins we replace are deleted first
    (exclude_actions would block our same-name re-registration too)."""
    from browser_use import Tools
    from browser_use.browser import BrowserSession

    tools = Tools(exclude_actions=list(EXCLUDED_ACTIONS))
    for name in REPLACED_ACTIONS:
        tools.registry.registry.actions.pop(name, None)

    @tools.action(description="Click an element by its index. Submit/buy/delete-like "
                              "clicks require the owner's phone approval and may be "
                              "DENIED — a denial is final, never retry it.")
    async def click(index: int, browser_session: BrowserSession):
        return await _gated_click_impl(index, browser_session)

    @tools.action(description="Send keyboard keys, e.g. Escape or ArrowDown. Enter may "
                              "submit a form, so it requires the owner's phone approval.")
    async def send_keys(keys: str, browser_session: BrowserSession, cdp_client):
        return await _gated_send_keys_impl(keys, browser_session, cdp_client)

    @tools.action(description="Look at the rendered page with the local vision model and "
                              "answer a visual question (colors, swatches, images, layout "
                              "— things the element list can't tell you).")
    async def look_at_page(question: str, browser_session: BrowserSession):
        return await _look_at_page_impl(question, browser_session)

    return tools


_EXTEND_SYSTEM = """
CRITICAL action format: one action per step, parameters ALWAYS nested in an object.
Correct: {"action":[{"navigate":{"url":"https://example.com"}}]}
Correct: {"action":[{"input":{"index":7,"text":"Alex Demo"}}]}
Correct: {"action":[{"click":{"index":12}}]}
Correct: {"action":[{"done":{"text":"summary of findings","success":true}}]}
Wrong: {"navigate":"https://example.com"} (parameters must be a nested object).

To TYPE into a text field, use the `input` action directly with the field's index — do
NOT click the field first, and never mistake a field's greyed placeholder text for a
button. Click is only for buttons, links, checkboxes, radios, and menu items.

You are working inside the owner's REAL browser: other tabs from their own browsing may
be open, and you CANNOT switch to or close them. Work ONLY in your own tab; open a new
tab (navigate with new_tab=true) when you need another page side by side. IGNORE every
pre-existing tab and its content completely — they are NOT part of your task. Your FIRST
action must be `navigate` to the page your task needs; if the task names no page,
navigate to a search engine. Re-read your task before every step; if the current page
does not serve it, navigate back to one that does.

Rules from the owner (non-negotiable):
- You will never be given passwords, one-time codes, or payment card numbers, and you
  must never invent or type placeholder credentials. If the page requires a login,
  verification code, or payment detail, immediately call done with success=false and
  say exactly what the human must do.
- Never attempt to solve or bypass a CAPTCHA, bot check, or paywall. Stop with done
  and report it.
- Some clicks and Enter presses trigger a phone approval to the owner; the run pauses
  until they answer. If an action is DENIED, that decision is final: never retry it —
  adjust the approach or finish with done.
- Finish with a concise, sourced answer: name the pages you used.
""".strip()


def _allowed_upload_paths() -> list[str]:
    """Allowlist for the built-in upload_file action: the profile resume, files the
    user sent over Telegram (UPLOAD_DIR), and task downloads. Nothing else."""
    paths: list[str] = []
    try:
        prof = {}
        if config.PROFILE_PATH.exists():
            prof = yaml.safe_load(config.PROFILE_PATH.read_text(encoding="utf-8")) or {}
        rp = str(prof.get("resume_path", "") or "").strip()
        try:  # profile_store override wins (Telegram-taught facts)
            import profile_store
            rp = profile_store.load().get("resume_path", rp) or rp
        except Exception:
            pass
        if rp:
            p = Path(rp).expanduser()
            if p.exists():
                paths.append(str(p))
    except Exception:
        pass
    for d in (config.UPLOAD_DIR, config.DOWNLOAD_DIR):
        try:
            if d.exists():
                paths.extend(str(f) for f in d.iterdir() if f.is_file())
        except Exception:
            pass
    return paths


async def _activate_tab(session, url: str) -> None:
    """Bring the sub-agent's working tab to the foreground so the user can WATCH it
    drive (CDP acts on background tabs invisibly otherwise). Best-effort, never raises."""
    if not url or url.startswith("edge://"):
        return
    try:
        import urllib.request
        for t in await session.get_tabs():
            if getattr(t, "url", "") == url:
                await asyncio.to_thread(
                    urllib.request.urlopen,
                    f"{config.CDP_URL}/json/activate/{t.target_id}", None, 3)
                break
    except Exception:
        pass


async def _close_owned_tabs(session, tabs_before) -> None:
    """Close only the tabs this run opened — never the user's own tabs."""
    if tabs_before is None:
        return
    try:
        from browser_use.browser.events import CloseTabEvent
        for t in await session.get_tabs():
            if t.target_id not in tabs_before:
                try:
                    await _dispatch(session, CloseTabEvent(target_id=t.target_id))
                except Exception:
                    pass
    except Exception:
        pass


async def _save_final_screenshot(history) -> str | None:
    """Copy the run's last step screenshot into our SCREENSHOT_DIR for the phone report."""
    try:
        shots = [s for s in (history.screenshot_paths(n_last=1) or []) if s]
        if not shots or not Path(shots[-1]).exists():
            return None
        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        dest = config.SCREENSHOT_DIR / f"web_agent_{int(time.time())}.png"
        shutil.copyfile(shots[-1], dest)
        return str(dest)
    except Exception:
        return None


async def web_agent(args: dict) -> str:
    """Outer tool: run one bounded browser-use mission. Never raises."""
    global _session, _last_screenshot
    if not config.BROWSER_USE_ENABLED:
        return "Error: the web agent is disabled (set BROWSER_USE_ENABLED=1 in .env)."
    task_text = str(args.get("task", "")).strip()
    if not task_text:
        return "Error: describe the web task."
    try:
        from browser_use import Agent, ChatOllama
        from browser_use.browser import BrowserProfile, BrowserSession
    except ImportError:
        return "Error: browser-use is not installed (pip install -r requirements.txt)."

    url = str(args.get("url", "")).strip()
    if not url:
        # Their directly_open_url regex misses dotless hosts (e.g. localhost); pull any
        # URL out of the task ourselves so the mission always starts on its own page.
        m = re.search(r"https?://[^\s'\")\]]+", task_text)
        url = m.group(0).rstrip(".,;") if m else ""
    initial_actions = None
    if url:
        task_text += f"\nWork on this page (already opened for you in a new tab): {url}"
        initial_actions = [{"navigate": {"url": url, "new_tab": True}}]
    upload_paths = _allowed_upload_paths()
    if upload_paths:
        listing = "\n".join(f"- {p}" for p in upload_paths[:12])
        task_text += ("\nFiles you may attach with upload_file (use the EXACT path; "
                      f"no other files are permitted):\n{listing}")
    try:
        if config.BROWSER_MODE == "edge":
            # Raises if Edge lacks the CDP port and the owner denies the relaunch;
            # the outer except turns that into a clean "Error: ..." string.
            await tools_browser._ensure_edge()
        session = BrowserSession(browser_profile=BrowserProfile(
            cdp_url=config.CDP_URL, is_local=True, keep_alive=True))
        _session = session
        _audit("web_agent_start", {"task": task_text[:200]})
        await session.start()
        try:  # tab etiquette: only tabs the sub-agent opens are ours to close later
            tabs_before = {t.target_id for t in await session.get_tabs()}
        except Exception:
            tabs_before = None

        async def _on_step_end(agent_obj):
            try:
                n = agent_obj.state.n_steps
                cur = await agent_obj.browser_session.get_current_page_url()
                _remember_source(cur)
                _audit("step", {"n": n, "url": cur})
                await _activate_tab(agent_obj.browser_session, cur)
                if _status_cb is not None:
                    await _status_cb(f"web_agent step {n}: {cur[:80]}")
            except Exception:
                pass

        agent = Agent(
            task=task_text,
            # ollama_options is essential: without num_ctx Ollama serves a 4k window,
            # browser-use's page states overflow it within ~3 steps, the task falls out
            # of context, and the model confabulates a remembered demo task instead.
            llm=ChatOllama(model=config.BROWSER_USE_MODEL, host=config.OLLAMA_HOST,
                           timeout=config.BROWSER_USE_STEP_TIMEOUT,
                           ollama_options={"num_ctx": config.NUM_CTX,
                                           "temperature": config.TEMPERATURE}),
            browser_session=session,
            tools=_build_tools(),
            use_vision=False,               # perception is DOM-based; look_at_page is the VLM path
            max_actions_per_step=1,         # local-model schema reliability
            max_failures=3,
            step_timeout=config.BROWSER_USE_STEP_TIMEOUT,
            extend_system_message=_EXTEND_SYSTEM,
            generate_gif=False,
            available_file_paths=upload_paths,
            initial_actions=initial_actions,  # mission opens in its OWN new tab
        )
        history = await agent.run(max_steps=config.BROWSER_USE_MAX_STEPS,
                                  on_step_end=_on_step_end)
        for u in dict.fromkeys(history.urls() or []):
            _remember_source(u)
        _last_screenshot = await _save_final_screenshot(history)
        await _close_owned_tabs(session, tabs_before)
        ok = history.is_successful()
        steps = history.number_of_steps()
        final = (history.final_result() or "").strip() or "(the web agent returned no final answer)"
        _audit("web_agent_done", {"steps": steps, "successful": ok}, ok=bool(ok))
        status = "completed" if ok else "finished without confirming success"
        result = f"[web_agent {status} in {steps} steps]\n{final}"
        if len(result) > config.TOOL_RESULT_MAX - 100:
            try:
                config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
                art = config.ARTIFACT_DIR / f"web_agent_{_task.id if _task else 'run'}_{int(time.time())}.txt"
                art.write_text(result, encoding="utf-8")
                _remember_artifact(art)
                result = result[:config.TOOL_RESULT_MAX - 100] + "\n…(full result attached as artifact)"
            except Exception:
                result = result[:config.TOOL_RESULT_MAX]
        return result
    except Exception as e:
        _audit("web_agent_failed", {"error": str(e)}, ok=False)
        return f"Error: web_agent failed: {e}"
    finally:
        s, _session = _session, None
        if s is not None:
            try:
                await s.stop()  # keep_alive=True -> detach only; the user's Edge stays open
            except Exception:
                pass


def last_screenshot() -> str | None:
    """Path of the sub-agent's final screenshot (for main._final_screenshot)."""
    if _last_screenshot and Path(_last_screenshot).exists():
        return _last_screenshot
    return None


async def shutdown() -> None:
    """Best-effort detach of a lingering session. Never closes the user's Edge."""
    global _session
    s, _session = _session, None
    if s is not None:
        try:
            await s.stop()
        except Exception:
            pass


# --- tool registry (empty when the feature flag is off: the model never sees it) ---
TOOLS: dict = {}
if config.BROWSER_USE_ENABLED:
    TOOLS["web_agent"] = {
        "schema": {
            "type": "function",
            "function": {
                "name": "web_agent",
                "description": (
                    "Delegate a LONG multi-page web mission (multi-site research, price "
                    "comparison, many-step form flows like a job application) to an "
                    "autonomous browser sub-agent driving the same Edge browser. It works "
                    "for several minutes and returns a sourced summary. Submits and other "
                    "high-impact clicks inside it still require the owner's phone approval. "
                    "For the user's current tab, simple lookups, or precise single-page "
                    "work, use the direct browser tools instead."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task": {"type": "string",
                                 "description": "Complete mission description, incl. what "
                                                "to return (e.g. 'compare RTX 5080 prices "
                                                "on newegg and bestbuy, report the cheapest')"},
                        "url": {"type": "string",
                                "description": "Optional starting URL"},
                    },
                    "required": ["task"],
                },
            },
        },
        "fn": web_agent,
    }
