"""Playwright tools for Jerry's real-Edge co-drive and test browser.

Async Playwright only. Module-level lazy singleton browser context reusing
one visible Chromium page. All tool fns return strings and never raise.
Irreversible clicks (per gate.is_irreversible_click) go through an async
confirm callback injected via configure().
"""
import asyncio
import csv
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

import ollama
from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

import config
import gate
import state

# --- module state ---
_pw = None
_browser = None
_context = None
_page = None
_last: dict[str, dict] = {}   # data-agent-id -> element info from last extraction
_cdp = False   # True when attached to the user's real Edge over CDP (co-drive)
_task: state.TaskRecord | None = None
_secret_resolver = None
_dialog_action: dict | None = None
_visual_target: dict | None = None
_visual_attempts = 0
_owned_pages: set = set()
_secret_fields: set[str] = set()
_secret_login_authorization: dict | None = None
_dom_failures: dict[str, int] = {}


def _cdp_available() -> bool:
    """Is the user's real Edge listening on the debug port? (co-drive launcher started it)"""
    try:
        with urllib.request.urlopen(config.CDP_URL + "/json/version", timeout=0.7) as r:
            return r.status == 200
    except Exception:
        return False

DENIED_MSG = (
    "User DENIED this action (or it timed out). Do not retry it. "
    "Ask what to change or finish with a summary."
)


async def _default_confirm(summary: str) -> bool:
    print("[tools_browser] No confirm callback configured — auto-DENYING gated action:\n" + summary)
    return False


_confirm_cb = _default_confirm


def configure(confirm=None, task=None, secret_resolver=None):
    """Inject per-task phone callbacks and audit context."""
    global _confirm_cb, _task, _secret_resolver, _visual_target, _visual_attempts
    global _dialog_action, _secret_login_authorization, _page
    _confirm_cb = confirm if confirm is not None else _default_confirm
    _task = task
    _secret_resolver = secret_resolver
    _visual_target = None
    _visual_attempts = 0
    _dialog_action = None
    _owned_pages.clear()
    _secret_fields.clear()
    _dom_failures.clear()
    _secret_login_authorization = None
    if task is not None and getattr(task, "source_url", None):
        _page = None  # force _ctx() to select the exact J-badge source tab


def _audit(action: str, details=None, *, ok: bool | None = None) -> None:
    if _task is not None:
        state.audit_event(_task, "browser", action, details, ok=ok)


def _remember_source(url: str) -> None:
    if _task is not None and url.startswith(("http://", "https://")):
        state.add_source(_task, url)


def _remember_artifact(path: str | Path) -> None:
    if _task is not None:
        state.add_artifact(_task, path)


def _edge_running() -> bool:
    if os.name != "nt":
        return False
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=4, check=False,
        )
        return "msedge.exe" in result.stdout.lower()
    except Exception:
        return False


def _edge_executable() -> str | None:
    found = shutil.which("msedge") or shutil.which("msedge.exe")
    if found:
        return found
    candidates = [
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
    ]
    return str(next((p for p in candidates if p.is_file()), "")) or None


def _launch_edge_sync() -> None:
    exe = _edge_executable()
    if not exe:
        raise RuntimeError("Microsoft Edge executable was not found")
    config.EDGE_CODRIVE_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        [exe, f"--remote-debugging-port={config.CDP_PORT}",
         f"--user-data-dir={config.EDGE_CODRIVE_PROFILE_DIR}",
         "--restore-last-session", "--no-first-run"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_for_cdp() -> bool:
    deadline = time.monotonic() + config.EDGE_START_TIMEOUT
    while time.monotonic() < deadline:
        if await asyncio.to_thread(_cdp_available):
            return True
        await asyncio.sleep(0.4)
    return False


async def _ensure_edge() -> None:
    """Start Edge, or approval-gate closing an already-running non-CDP session."""
    if await asyncio.to_thread(_cdp_available):
        return
    running = await asyncio.to_thread(_edge_running)
    if running:
        summary = (
            "Close and relaunch Microsoft Edge with Jerry co-drive enabled. "
            "Edge will restore the last session, but unsaved page state may be lost."
        )
        if not await _confirm_cb(summary):
            raise RuntimeError("Edge is open without co-drive and relaunch was denied")
        _audit("edge_relaunch_approved", {"reason": "CDP unavailable"}, ok=True)
        result = await asyncio.to_thread(
            subprocess.run,
            ["taskkill", "/IM", "msedge.exe", "/F"],
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode not in (0, 128):
            raise RuntimeError("could not close Edge for co-drive relaunch")
        await asyncio.sleep(1.5)
    await asyncio.to_thread(_launch_edge_sync)
    if not await _wait_for_cdp():
        raise RuntimeError(
            f"Edge started but its co-drive endpoint did not appear on port {config.CDP_PORT}"
        )
    _audit("edge_started", {"port": config.CDP_PORT}, ok=True)


async def _ctx():
    """Return the active page. Production uses Edge; tests explicitly select owned mode."""
    global _pw, _browser, _context, _page, _cdp
    if _page is not None and not _page.is_closed():
        if not (_task is not None and _task.source_url):
            return _page
        wanted = _task.source_url.rstrip("/")
        if (_page.url or "").rstrip("/") == wanted:
            return _page
    if _context is None:
        _pw = await async_playwright().start()
        if config.BROWSER_MODE == "edge":
            await _ensure_edge()
            _browser = await _pw.chromium.connect_over_cdp(config.CDP_URL)
            if not _browser.contexts:
                raise RuntimeError("Edge exposed CDP but no browser context was available")
            _context = _browser.contexts[0]
            _cdp = True
        elif config.BROWSER_MODE == "owned":
            _context = await _pw.chromium.launch_persistent_context(
                user_data_dir=str(config.BROWSER_PROFILE_DIR),
                headless=False,
                args=[
                    "--start-maximized",
                    "--disable-features=PasswordManagerOnboarding,AutofillServerCommunication",
                ],
                ignore_default_args=["--enable-automation"],
                no_viewport=True,
            )
            _cdp = False
        else:
            raise RuntimeError("BROWSER_MODE must be 'edge' or 'owned'")
    # A J-badge task must begin on the tab that created it, not whichever tab was last.
    if _context.pages:
        wanted = (_task.source_url if _task is not None else None) or ""
        exact = next((p for p in _context.pages
                      if (p.url or "").rstrip("/") == wanted.rstrip("/")), None)
        prefix = next((p for p in _context.pages
                       if wanted and (p.url or "").startswith(wanted.split("#", 1)[0])), None)
        _page = exact or prefix or _context.pages[-1]
    else:
        _page = await _context.new_page()
    return _page


async def shutdown():
    """Disconnect (CDP: never close the user's Edge) or close our own browser."""
    global _pw, _browser, _context, _page, _cdp
    try:
        if _cdp and _context is not None:
            # Stopping Playwright disconnects CDP. Never call browser.close() on real Edge.
            pass
        elif _context is not None:
            await _context.close()
    except Exception:
        pass
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _pw = _browser = _context = _page = None
    _cdp = False
    _last.clear()
    _owned_pages.clear()
    _secret_fields.clear()


def page_or_none():
    """The live Page, or None if the browser was never started / is closed."""
    if _page is not None and not _page.is_closed():
        return _page
    return None


# --- THE EXTRACTOR (evaluated in the top frame and every iframe) ---
_EXTRACT_JS = """
(prefix) => {
  const isVisible = (el) => el.offsetParent !== null || el.getClientRects().length > 0;
  const isFormField = (el) => ['INPUT', 'SELECT', 'TEXTAREA'].includes(el.tagName);
  const textOf = (el) => ((el.innerText || el.textContent || '')).trim().replace(/\\s+/g, ' ').slice(0, 80);

  const cand = new Set();
  document.querySelectorAll(
    'input:not([type=hidden]), select, textarea, button, a[href], [role=button], [onclick]'
  ).forEach((el) => cand.add(el));
  document.querySelectorAll('div, span').forEach((el) => {
    try { if (getComputedStyle(el).cursor === 'pointer') cand.add(el); } catch (e) {}
  });

  const kept = [];
  for (const el of cand) {
    // Hidden radios/checkboxes often have a visible associated label (Tesla/React design
    // systems). Keep form fields so Jerry can operate their visible label safely.
    if (!isVisible(el) && !isFormField(el)) continue;
    if (el.disabled) continue;
    if (!isFormField(el)) {
      // dedupe nested clickables — keep the outermost one
      let p = el.parentElement, nested = false;
      while (p) {
        if (cand.has(p) && !isFormField(p)) { nested = true; break; }
        p = p.parentElement;
      }
      if (nested) continue;
    }
    kept.push(el);
  }

  const labelOf = (el) => {
    if (el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l && textOf(l)) return textOf(l);
    }
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    const wrap = el.closest('label');
    if (wrap && textOf(wrap)) return textOf(wrap);
    const nm = el.getAttribute('name');
    if (nm) return nm;
    if (el.tagName === 'BUTTON' || el.tagName === 'A' || el.getAttribute('role') === 'button'
        || el.tagName === 'DIV' || el.tagName === 'SPAN') {
      if (textOf(el)) return textOf(el);
    }
    const sib = el.previousElementSibling;
    if (sib && textOf(sib)) return textOf(sib);
    return '';
  };

  const usedIds = new Set();
  const hash = (s) => {
    let h = 2166136261;
    for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); }
    return (h >>> 0).toString(36);
  };
  const out = [];
  for (const el of kept) {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : '')).toLowerCase();
    const nameValue = (el.getAttribute('name') || '') + '|' + (el.getAttribute('value') || '');
    const identity = el.id || el.getAttribute('data-id') || (nameValue !== '|' ? nameValue : '') ||
      (el.getAttribute('aria-label') || '') || (tag + '|' + textOf(el));
    let id = prefix + 's' + hash(identity);
    let collision = 1;
    while (usedIds.has(id)) { id = prefix + 's' + hash(identity + '|' + collision++); }
    usedIds.add(id);
    el.setAttribute('data-agent-id', id);
    let options = '';
    if (tag === 'select') {
      options = Array.from(el.options).slice(0, 12).map((o) => o.label || o.value).join('|');
    }
    out.push({
      id: id,
      dom_id: el.id || '',
      name: el.getAttribute('name') || '',
      data_id: el.getAttribute('data-id') || '',
      tag: tag,
      type: type,
      label: labelOf(el).slice(0, 80),
      text: textOf(el),
      aria_label: (el.getAttribute('aria-label') || '').trim(),
      value: ('value' in el && el.value != null) ? String(el.value).slice(0, 120) : '',
      checked: (type === 'checkbox' || type === 'radio') ? !!el.checked : null,
      options: options,
      in_form: el.closest('form') !== null,
    });
  }
  return out;
}
"""


async def _extract():
    """Run the extractor in every frame; refresh _last; return (elements, title, url)."""
    page = await _ctx()
    _last.clear()
    elements = []
    for i, frame in enumerate(page.frames):
        prefix = "e" if frame == page.main_frame else f"f{i}e"
        try:
            els = await frame.evaluate(_EXTRACT_JS, prefix)
        except Exception:
            continue  # frame may have navigated away / be inaccessible
        for el in els:
            el["frame_index"] = i
            if el.get("type") == "password" or el.get("id") in _secret_fields:
                el["value"] = "[REDACTED]"
            el["is_submit_candidate"] = gate.is_irreversible_click(el)
            _last[el["id"]] = el
            elements.append(el)
    try:
        title = await page.title()
    except Exception:
        title = ""
    return elements, title, page.url


def _digest(elements, page_title, url) -> str:
    """One line per element, capped at ~TOOL_RESULT_MAX*1.5 chars."""
    lines = [f"Page: {page_title} — {url}"]
    limit = int(config.TOOL_RESULT_MAX * 1.5)
    used = len(lines[0])
    shown = 0
    for el in elements:
        eid, tag, typ = el["id"], el["tag"], el.get("type") or ""
        label = el.get("label") or ""
        if tag == "input" and typ in ("checkbox", "radio"):
            line = f'[{eid}] {typ} "{label}" {"checked" if el.get("checked") else "unchecked"}'
        elif tag == "input":
            line = f'[{eid}] {typ or "text"} input "{label}" value="{el.get("value", "")}"'
        elif tag == "select":
            line = f'[{eid}] select "{label}" options: {el.get("options", "")} value="{el.get("value", "")}"'
        elif tag == "textarea":
            line = f'[{eid}] textarea "{label}" value="{el.get("value", "")}"'
        else:
            line = f'[{eid}] {tag.upper()} "{label or el.get("text", "")}"'
        if el.get("is_submit_candidate"):
            line += " [SUBMIT — requires approval]"
        if used + len(line) + 1 > limit:
            lines.append(f"…and {len(elements) - shown} more elements")
            break
        lines.append(line)
        used += len(line) + 1
        shown += 1
    return "\n".join(lines)


async def _settle(page):
    """Let JS-heavy pages finish rendering before we read them (live-observed bug:
    the Tesla configurator extracted before it painted). Best-effort, never raises."""
    try:
        await page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    try:
        await page.wait_for_timeout(800)
    except Exception:
        pass


# Markers of a CAPTCHA / human-verification / bot wall. Boundary: the agent must
# STOP and hand back to the user — never attempt to solve or automate around these.
_CHALLENGE_IFRAME = ("recaptcha", "hcaptcha", "turnstile", "arkoselabs", "funcaptcha", "geo.captcha")
_CHALLENGE_TEXT = (
    "verify you are human", "verify you're human", "are you a robot", "i'm not a robot",
    "unusual traffic", "access denied", "checking your browser", "complete the captcha",
    "press and hold", "confirm you are human", "enable javascript and cookies",
)
CHALLENGE_MSG = (
    "STOP: this page is showing a CAPTCHA or human-verification/bot check. "
    "Per policy the agent must NOT solve or bypass it. Call ask_user to tell the user "
    "to complete the verification themselves in their browser, then say 'continue' — and wait."
)


async def _challenge_on(page) -> bool:
    """True if the current page is a CAPTCHA / verification / bot wall."""
    try:
        for f in page.frames:
            u = (f.url or "").lower()
            if any(m in u for m in _CHALLENGE_IFRAME):
                return True
        body = (await page.evaluate("() => document.body ? document.body.innerText : ''") or "").lower()
        head = body[:1500]
        return any(t in head for t in _CHALLENGE_TEXT)
    except Exception:
        return False


async def _fresh_digest() -> str:
    page = await _ctx()
    await _settle(page)
    if await _challenge_on(page):
        return CHALLENGE_MSG
    elements, title, url = await _extract()
    return _digest(elements, title, url)


def _frame_for(el):
    """Frame the element was extracted from (frames order is stable between calls)."""
    page = page_or_none()
    if page is None:
        return None
    idx = el.get("frame_index", 0)
    frames = page.frames
    return frames[idx] if idx < len(frames) else page.main_frame


def _form_values() -> list[str]:
    """'label = value' strings for form fields from the last extraction."""
    vals = []
    for el in _last.values():
        if not el.get("in_form"):
            continue
        tag, typ = el.get("tag"), (el.get("type") or "").lower()
        if tag not in ("input", "select", "textarea") or typ in ("submit", "button", "reset"):
            continue
        label = el.get("label") or el["id"]
        if typ in ("checkbox", "radio"):
            vals.append(f"{label} = {'checked' if el.get('checked') else 'unchecked'}")
        else:
            vals.append(f"{label} = {el.get('value', '')}")
    return vals


def _secret_locators() -> list:
    locators = []
    for field_id in _secret_fields:
        el = _last.get(field_id)
        if el is not None:
            frame = _frame_for(el)
            if frame is not None:
                locators.append(frame.locator(f'[data-agent-id="{field_id}"]'))
    return locators


def _control_locator(frame, el: dict, field_id: str):
    """Use a DOM-stable attribute when available, falling back to Jerry's marker."""
    dom_id = str(el.get("dom_id") or "").replace('"', '\\"')
    if dom_id:
        return frame.locator(f'[id="{dom_id}"]')
    data_id = str(el.get("data_id") or "").replace('"', '\\"')
    if data_id:
        return frame.locator(f'[data-id="{data_id}"]')
    return frame.locator(f'[data-agent-id="{field_id}"]')


async def _clickable_control(frame, el: dict, field_id: str):
    """Return the visible label for hidden radios/checkboxes; otherwise the control itself."""
    loc = _control_locator(frame, el, field_id)
    typ = (el.get("type") or "").lower()
    if typ in ("radio", "checkbox"):
        dom_id = str(el.get("dom_id") or "").replace('"', '\\"')
        if dom_id:
            label = frame.locator(f'label[for="{dom_id}"]')
            if await label.count() == 1 and await label.is_visible():
                return label, loc
    return loc, loc


_FALSEY = {"false", "0", "no", "off", "unchecked", "none", ""}


# --- tool fns (never raise) ---

async def browser_goto(args: dict) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "Error: missing url"
    try:
        await _ctx()
        # Co-drive: if the user already has this page open, act on THAT tab rather
        # than navigating a different one out from under them.
        existing = await _page_for_url(url)
        if existing is not None:
            await _select_page(existing)
            _remember_source(existing.url)
            _audit("navigate_existing", {"url": existing.url})
            return await _fresh_digest()
        page = await _ctx()
        await page.goto(url, wait_until="load", timeout=20000)
        _remember_source(page.url)
        _audit("navigate", {"url": page.url}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not open {url}: {e}"


async def read_page(args: dict) -> str:
    try:
        page = await _ctx()
        await _settle(page)
        if await _challenge_on(page):
            return CHALLENGE_MSG
        title = await page.title()
        body = await page.evaluate("() => document.body ? document.body.innerText : ''")
        text = f"{title}\n{body.strip()}"
        _remember_source(page.url)
        _audit("read", {"url": page.url, "characters": len(text)})
        return text[: config.PAGE_TEXT_MAX]
    except Exception as e:
        return f"Error: could not read page: {e}"


async def extract_form_fields(args: dict) -> str:
    try:
        return await _fresh_digest()
    except Exception as e:
        return f"Error: extraction failed: {e}"


async def find_elements(args: dict) -> str:
    """Search the complete extracted control set, including entries omitted from the digest."""
    try:
        page = await _ctx()
        await _settle(page)
        if await _challenge_on(page):
            return CHALLENGE_MSG
        await _extract()
        query = str(args.get("query", "")).strip().lower()
        group = str(args.get("group", "")).strip().lower()
        if not query and not group:
            return "Error: provide query or group"
        matches = []
        for field_id, el in _last.items():
            haystack = " ".join(str(el.get(k) or "") for k in
                                ("label", "text", "aria_label", "name", "value")).lower()
            if query and query not in haystack:
                continue
            if group and group not in str(el.get("name") or "").lower():
                continue
            matches.append({
                "field_id": field_id,
                "tag": el.get("tag"),
                "type": el.get("type"),
                "group": el.get("name"),
                "label": el.get("label") or el.get("aria_label") or el.get("text"),
                "value": el.get("value"),
                "checked": el.get("checked"),
            })
        _audit("find_elements", {"query": query, "group": group,
                                 "matches": len(matches)}, ok=True)
        return json.dumps(matches[:30], ensure_ascii=False)
    except Exception as e:
        return f"Error: element search failed: {e}"


async def choose_option(args: dict) -> str:
    """Choose a radio/checkbox/button by semantic label rather than a transient element id."""
    try:
        page = await _ctx()
        await _settle(page)
        if await _challenge_on(page):
            return CHALLENGE_MSG
        await _extract()
        label = str(args.get("label", "")).strip()
        group = str(args.get("group", "")).strip()
        if not label:
            return "Error: label is required"
        eligible = []
        group_choices = []
        for field_id, el in _last.items():
            if (el.get("type") or "").lower() not in ("radio", "checkbox"):
                continue
            if group and str(el.get("name") or "").lower() != group.lower():
                continue
            display = str(el.get("label") or el.get("aria_label") or "")
            group_choices.append(display)
            if display.lower() == label.lower():
                eligible.insert(0, (field_id, el, True))
            elif label.lower() in display.lower():
                eligible.append((field_id, el, False))
        exact = [x for x in eligible if x[2]]
        selected = exact or eligible
        if len(selected) != 1:
            reason = "no exact choice" if not selected else "ambiguous choice"
            return (f"Error: {reason} for {label!r} in group {group or '(any)'}. "
                    f"Available choices: {', '.join(x for x in group_choices if x) or '(none)'}")
        return await click_element({"field_id": selected[0][0]})
    except Exception as e:
        return f"Error: could not choose option: {e}"


async def fill_field(args: dict) -> str:
    field_id = str(args.get("field_id") or "").strip()
    value = args.get("value", "")
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        frame = _frame_for(el)
        loc = _control_locator(frame, el, field_id)
        if (el.get("type") or "").lower() in ("checkbox", "radio"):
            desired = str(value).strip().lower() not in _FALSEY
            clickable, native = await _clickable_control(frame, el, field_id)
            current = await native.is_checked()
            if current != desired:
                await clickable.click(timeout=15000)
            await (await _ctx()).wait_for_timeout(300)
            refreshed = _control_locator(frame, el, field_id)
            if await refreshed.is_checked() != desired:
                raise RuntimeError(f"{el.get('label') or field_id} did not reach the requested state")
        else:
            await loc.fill(str(value), timeout=15000)
        _audit("fill", {"field": field_id, "label": el.get("label"), "value": value}, ok=True)
        return f"Filled {field_id}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not fill {field_id}: {e}"


async def select_option(args: dict) -> str:
    field_id = str(args.get("field_id") or "").strip()
    option = str(args.get("option", ""))
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        frame = _frame_for(el)
        loc = frame.locator(f'[data-agent-id="{field_id}"]')
        try:
            await loc.select_option(label=option, timeout=15000)
        except Exception:
            await loc.select_option(value=option, timeout=15000)
        _audit("select", {"field": field_id, "label": el.get("label"), "option": option}, ok=True)
        return f"Selected \"{option}\" in {field_id}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not select \"{option}\" in {field_id}: {e}"


async def upload_file(args: dict) -> str:
    field_id = str(args.get("field_id") or "").strip()
    path = str(args.get("path") or "")
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        import tools_fs
        p = tools_fs._safe(path)  # same sandbox rule as every other file access
        if not p.is_file():
            return f"Error: {p} does not exist or is not a file."
        frame = _frame_for(el)
        loc = frame.locator(f'[data-agent-id="{field_id}"]')
        await loc.set_input_files(str(p), timeout=15000)
        _audit("upload", {"field": field_id, "file": p.name}, ok=True)
        return f"Attached {p.name} to {field_id}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not upload to {field_id}: {e}"


async def click_element(args: dict) -> str:
    global _secret_login_authorization
    field_id = str(args.get("field_id") or "").strip()
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        page = await _ctx()
        login_authorized = False
        if _secret_login_authorization is not None:
            auth = _secret_login_authorization
            click_label = " ".join(
                str(el.get(k) or "") for k in ("label", "text", "aria_label"))
            same_domain = auth["domain"] == urlsplit(page.url).netloc.lower()
            login_label = re.search(r"\b(log ?in|sign ?in|verify|continue|next)\b", click_label, re.I)
            if auth["expires"] > time.monotonic() and same_domain and login_label:
                login_authorized = True
                _secret_login_authorization = None
                _audit("login_authorized_by_secret_reply",
                       {"domain": auth["domain"], "field": auth["field"]}, ok=True)
        if gate.is_irreversible_click(el) and not login_authorized:
            el_for_gate = dict(el)
            try:
                title = await page.title()
                el_for_gate["page"] = f"{title or '(untitled)'} — {page.url}"
            except Exception:
                el_for_gate["page"] = page.url
            summary = gate.summarize_submission(el_for_gate, _form_values())
            approved = await _confirm_cb(summary)
            if not approved:
                return DENIED_MSG
        frame = _frame_for(el)
        loc, native = await _clickable_control(frame, el, field_id)
        if _dialog_action is not None:
            page.once("dialog", _handle_next_dialog)
        await loc.click(timeout=15000)
        verified = ""
        if (el.get("type") or "").lower() in ("radio", "checkbox"):
            await page.wait_for_timeout(300)
            refreshed = _control_locator(frame, el, field_id)
            if not await refreshed.is_checked():
                raise RuntimeError(f"{el.get('label') or field_id} was clicked but is not selected")
            verified = f" Verified selected: {el.get('label') or field_id}."
        _dom_failures.pop(field_id, None)
        _audit("click", {"field": field_id, "label": el.get("label")}, ok=True)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeoutError:
            pass
        return f"Clicked {field_id}.{verified}\n" + await _fresh_digest()
    except Exception as e:
        _dom_failures[field_id] = _dom_failures.get(field_id, 0) + 1
        if (_dom_failures[field_id] >= 2 and config.VISION_ENABLED
                and not args.get("_no_visual")):
            target = el.get("label") or el.get("text") or el.get("aria_label") or field_id
            vision = await visual_inspect({"target": target})
            if not vision.startswith("Error"):
                return await visual_click({"_no_visual": True})
        return f"Error: could not click {field_id}: {e}"


async def screenshot_page(args: dict) -> str:
    try:
        page = await _ctx()
        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.SCREENSHOT_DIR / f"page_{int(time.time())}.png"
        await page.screenshot(path=str(path), mask=_secret_locators())
        _audit("screenshot", {"path": str(path)}, ok=True)
        return f"screenshot saved: {path}"
    except Exception as e:
        return f"Error: screenshot failed: {e}"


# --- tabs (mainly for co-drive: act on the tab the user is looking at) ---
async def _select_page(page):
    """Make `page` the active target and bring it to front."""
    global _page
    _page = page
    try:
        await page.bring_to_front()
    except Exception:
        pass
    return page


async def _page_for_url(url: str):
    """Find an open tab whose URL matches (exact, then prefix). None if no match."""
    await _ctx()
    if not url or _context is None:
        return None
    u = url.rstrip("/")
    for p in _context.pages:
        if (p.url or "").rstrip("/") == u:
            return p
    for p in _context.pages:
        if (p.url or "").startswith(url[:60]):
            return p
    return None


async def list_tabs(args: dict) -> str:
    try:
        await _ctx()
        pages = _context.pages if _context else []
        if not pages:
            return "No open tabs."
        lines = []
        for i, p in enumerate(pages):
            try:
                title = (await p.title())[:60]
            except Exception:
                title = ""
            mark = " <- current" if p is _page else ""
            lines.append(f"[{i}] {title} — {p.url}{mark}")
        return "\n".join(lines)
    except Exception as e:
        return f"Error: could not list tabs: {e}"


async def switch_tab(args: dict) -> str:
    try:
        await _ctx()
        pages = _context.pages if _context else []
        idx = int(args.get("index", -1))
        if not (0 <= idx < len(pages)):
            return f"Error: tab index {idx} out of range (0..{len(pages) - 1}). Call list_tabs."
        await _select_page(pages[idx])
        _remember_source(pages[idx].url)
        _audit("switch_tab", {"index": idx, "url": pages[idx].url}, ok=True)
        return f"Switched to tab {idx}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not switch tab: {e}"


async def new_tab(args: dict) -> str:
    try:
        await _ctx()
        url = str(args.get("url") or "").strip()
        page = await _context.new_page()
        _owned_pages.add(page)
        await _select_page(page)
        if url:
            try:
                await page.goto(url, wait_until="load", timeout=20000)
            except Exception as e:
                return f"Opened a new tab but could not load {url}: {e}"
        _remember_source(page.url)
        _audit("new_tab", {"url": page.url}, ok=True)
        return f"Opened new tab.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not open new tab: {e}"


async def browser_back(args: dict) -> str:
    try:
        page = await _ctx()
        await page.go_back(wait_until="load", timeout=20000)
        _remember_source(page.url)
        _audit("back", {"url": page.url}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not go back: {e}"


async def browser_forward(args: dict) -> str:
    try:
        page = await _ctx()
        await page.go_forward(wait_until="load", timeout=20000)
        _remember_source(page.url)
        _audit("forward", {"url": page.url}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not go forward: {e}"


async def reload_page(args: dict) -> str:
    try:
        page = await _ctx()
        await page.reload(wait_until="load", timeout=20000)
        _audit("reload", {"url": page.url}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not reload: {e}"


async def scroll_page(args: dict) -> str:
    try:
        page = await _ctx()
        amount = max(-4000, min(4000, int(args.get("amount", 700))))
        await page.mouse.wheel(0, amount)
        await page.wait_for_timeout(250)
        _audit("scroll", {"amount": amount}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not scroll: {e}"


async def press_key(args: dict) -> str:
    try:
        page = await _ctx()
        key = str(args.get("key", "")).strip()
        if not key or len(key) > 40:
            return "Error: key is missing or too long"
        await page.keyboard.press(key)
        _audit("key", {"key": key}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not press key: {e}"


async def hover_element(args: dict) -> str:
    field_id = str(args.get("field_id", "")).strip()
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        await _frame_for(el).locator(f'[data-agent-id="{field_id}"]').hover(timeout=15000)
        _audit("hover", {"field": field_id, "label": el.get("label")}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not hover {field_id}: {e}"


async def scroll_to_element(args: dict) -> str:
    field_id = str(args.get("field_id", "")).strip()
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        await _frame_for(el).locator(f'[data-agent-id="{field_id}"]').scroll_into_view_if_needed(
            timeout=15000)
        await (await _ctx()).wait_for_timeout(250)
        _audit("scroll_to_element", {"field": field_id, "label": el.get("label")}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not scroll to {field_id}: {e}"


async def drag_and_drop(args: dict) -> str:
    source_id = str(args.get("source_id", "")).strip()
    target_id = str(args.get("target_id", "")).strip()
    source, target = _last.get(source_id), _last.get(target_id)
    if source is None or target is None:
        return "Error: source or target is not in the latest extraction."
    try:
        src = _frame_for(source).locator(f'[data-agent-id="{source_id}"]')
        dst = _frame_for(target).locator(f'[data-agent-id="{target_id}"]')
        await src.drag_to(dst, timeout=15000)
        _audit("drag", {"source": source_id, "target": target_id}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not drag {source_id} to {target_id}: {e}"


async def close_tab(args: dict) -> str:
    global _page
    try:
        await _ctx()
        pages = _context.pages if _context else []
        idx = int(args.get("index", -1))
        if not (0 <= idx < len(pages)):
            return f"Error: tab index {idx} out of range."
        target = pages[idx]
        if target not in _owned_pages:
            return "Error: Jerry only closes tabs it opened for the current session."
        url = target.url
        _owned_pages.discard(target)
        await target.close()
        pages = _context.pages if _context else []
        _page = pages[-1] if pages else await _context.new_page()
        await _select_page(_page)
        _audit("close_tab", {"index": idx, "url": url}, ok=True)
        return await list_tabs({})
    except Exception as e:
        return f"Error: could not close tab: {e}"


async def prepare_dialog(args: dict) -> str:
    global _dialog_action
    action = str(args.get("action", "dismiss")).lower()
    if action not in ("accept", "dismiss"):
        return "Error: dialog action must be accept or dismiss"
    _dialog_action = {"action": action, "text": str(args.get("text", ""))}
    _audit("dialog_prepared", {"action": action}, ok=True)
    return f"The next JavaScript dialog will be {action}ed."


async def _handle_next_dialog(dialog) -> None:
    global _dialog_action
    action = _dialog_action or {"action": "dismiss", "text": ""}
    _dialog_action = None
    if action["action"] == "accept":
        await dialog.accept(action.get("text") or None)
    else:
        await dialog.dismiss()
    _audit("dialog", {"type": dialog.type, "action": action["action"]}, ok=True)


async def download_element(args: dict) -> str:
    field_id = str(args.get("field_id", "")).strip()
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction."
    try:
        page = await _ctx()
        if gate.is_irreversible_click(el):
            summary = gate.summarize_submission({**el, "page": page.url}, _form_values())
            if not await _confirm_cb(summary):
                return DENIED_MSG
        loc = _frame_for(el).locator(f'[data-agent-id="{field_id}"]')
        config.DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
        async with page.expect_download(timeout=20000) as info:
            await loc.click(timeout=15000)
        download = await info.value
        safe_name = Path(download.suggested_filename).name
        path = config.DOWNLOAD_DIR / safe_name
        await download.save_as(str(path))
        _remember_artifact(path)
        _audit("download", {"field": field_id, "file": safe_name}, ok=True)
        return f"Downloaded: {path}"
    except Exception as e:
        return f"Error: download failed: {e}"


async def fill_secret_field(args: dict) -> str:
    global _secret_login_authorization
    field_id = str(args.get("field_id", "")).strip()
    handle = str(args.get("secret_handle", "")).strip()
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction."
    if _secret_resolver is None:
        return "Error: Telegram secret handling is not configured."
    try:
        secret = await _secret_resolver(handle, task_id=_task.id if _task else None)
        await _frame_for(el).locator(f'[data-agent-id="{field_id}"]').fill(secret, timeout=15000)
        _secret_fields.add(field_id)
        label = str(el.get("label") or "")
        looks_like_login_secret = (
            (el.get("type") or "").lower() == "password"
            or bool(re.search(r"password|one[- ]?time|verification code|\botp\b", label, re.I))
        ) and not bool(re.search(r"cvv|cvc|card security", label, re.I))
        if looks_like_login_secret:
            page = await _ctx()
            _secret_login_authorization = {
                "domain": urlsplit(page.url).netloc.lower(),
                "field": field_id,
                "expires": time.monotonic() + config.CONFIRM_TIMEOUT,
            }
        _audit("fill_secret", {"field": field_id, "label": el.get("label"), "value": "[REDACTED]"}, ok=True)
        del secret
        return f"Filled protected value in {field_id}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not fill protected value: {e}"


async def scrape_page(args: dict) -> str:
    try:
        page = await _ctx()
        await _settle(page)
        if await _challenge_on(page):
            return CHALLENGE_MSG
        mode = str(args.get("mode", "summary")).lower()
        output_format = str(args.get("format", "auto")).lower()
        if mode not in ("summary", "links", "rows"):
            return "Error: mode must be summary, links, or rows"
        if output_format not in ("auto", "csv", "json"):
            return "Error: format must be auto, csv, or json"
        limit = max(1, min(200, int(args.get("max_items", 100))))
        selector = str(args.get("selector", "")).strip()
        data = await page.evaluate(
            """({mode, limit, selector}) => {
              const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
              const out = {title: document.title, url: location.href, mode, items: []};
              if (mode === 'summary') {
                out.text = clean(document.body ? document.body.innerText : '').slice(0, 12000);
                return out;
              }
              if (mode === 'links') {
                out.items = Array.from(document.querySelectorAll(selector || 'a[href]'))
                  .slice(0, limit).map(a => ({text: clean(a.innerText || a.textContent).slice(0, 300), url: a.href}));
                return out;
              }
              const tables = Array.from(document.querySelectorAll(selector || 'table'));
              for (const table of tables) {
                const trs = Array.from(table.querySelectorAll('tr'));
                if (!trs.length) continue;
                let headers = Array.from(trs[0].querySelectorAll('th,td')).map((c,i) => clean(c.innerText) || `column_${i+1}`);
                for (const tr of trs.slice(1)) {
                  const cells = Array.from(tr.querySelectorAll('th,td')).map(c => clean(c.innerText));
                  if (!cells.length) continue;
                  const row = {}; cells.forEach((v,i) => row[headers[i] || `column_${i+1}`] = v);
                  out.items.push(row); if (out.items.length >= limit) return out;
                }
              }
              if (!out.items.length) {
                const cards = Array.from(document.querySelectorAll(selector || 'article, [role=listitem], li'));
                out.items = cards.slice(0, limit).map(el => ({
                  text: clean(el.innerText || el.textContent).slice(0, 1000),
                  url: (el.querySelector('a[href]') || {}).href || ''
                }));
              }
              return out;
            }""",
            {"mode": mode, "limit": limit, "selector": selector},
        )
        _remember_source(page.url)
        _audit("scrape", {"mode": mode, "url": page.url, "items": len(data.get("items", []))}, ok=True)
        if mode in ("links", "rows") and data.get("items"):
            config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            items = data["items"]
            nested = any(any(isinstance(v, (dict, list)) for v in item.values()) for item in items)
            chosen = "json" if output_format == "json" or (output_format == "auto" and nested) else "csv"
            path = config.ARTIFACT_DIR / f"task_{_task.id if _task else int(time.time())}_{mode}.{chosen}"
            if chosen == "json":
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                keys = []
                for item in items:
                    for key in item:
                        if key not in keys:
                            keys.append(key)
                with open(path, "w", encoding="utf-8-sig", newline="") as f:
                    writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(items)
            _remember_artifact(path)
            data["artifact"] = str(path)
        return json.dumps(data, ensure_ascii=False)[: max(config.TOOL_RESULT_MAX * 2, 4000)]
    except Exception as e:
        return f"Error: scrape failed: {e}"


async def _unload_model(client, model: str) -> None:
    try:
        await client.generate(model=model, prompt="", keep_alive=0)
    except Exception:
        pass


async def _add_vision_overlay(page) -> list[dict]:
    """Temporarily mark visible DOM controls so the VLM can cross-reference vision and DOM."""
    return await page.evaluate(
        """() => {
          document.getElementById('jerry-vision-overlay')?.remove();
          const root = document.createElement('div');
          root.id = 'jerry-vision-overlay';
          root.style.cssText = 'position:fixed;inset:0;z-index:2147483646;pointer-events:none;';
          const out = [];
          for (const el of Array.from(document.querySelectorAll('[data-agent-id]')).slice(0, 100)) {
            const r = el.getBoundingClientRect();
            if (r.width < 2 || r.height < 2 || r.bottom < 0 || r.right < 0 ||
                r.top > innerHeight || r.left > innerWidth) continue;
            const id = el.getAttribute('data-agent-id');
            const label = (el.getAttribute('aria-label') || el.innerText ||
              el.getAttribute('placeholder') || el.getAttribute('name') || '').trim().replace(/\s+/g,' ').slice(0,80);
            const box = document.createElement('div');
            box.style.cssText = `position:absolute;left:${Math.max(0,r.left)}px;top:${Math.max(0,r.top)}px;` +
              `width:${r.width}px;height:${r.height}px;border:2px solid #ff3158;box-sizing:border-box;`;
            const badge = document.createElement('span');
            badge.textContent = id;
            badge.style.cssText = 'position:absolute;left:0;top:0;background:#ff3158;color:white;' +
              'font:700 12px sans-serif;padding:1px 3px;transform:translateY(-100%);';
            box.appendChild(badge); root.appendChild(box);
            out.push({id, tag:el.tagName.toLowerCase(), label,
              x:Math.round(r.left), y:Math.round(r.top), width:Math.round(r.width), height:Math.round(r.height)});
          }
          document.documentElement.appendChild(root);
          return out;
        }"""
    )


async def _remove_vision_overlay(page) -> None:
    try:
        await page.evaluate("() => document.getElementById('jerry-vision-overlay')?.remove()")
    except Exception:
        pass


async def visual_inspect(args: dict) -> str:
    global _visual_target, _visual_attempts
    if not config.VISION_ENABLED:
        return "Error: local vision is disabled."
    if _visual_attempts >= config.VISION_MAX_ATTEMPTS:
        return "Error: visual fallback already failed twice; ask the user for help."
    try:
        page = await _ctx()
        if await _challenge_on(page):
            return CHALLENGE_MSG
        description = str(args.get("target", "")).strip()
        if not description:
            return "Error: describe the visual target"
        _visual_attempts += 1
        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.SCREENSHOT_DIR / f"vision_{int(time.time() * 1000)}.png"
        await _extract()
        candidates = await _add_vision_overlay(page)
        try:
            await page.screenshot(path=str(path), mask=_secret_locators())
        finally:
            await _remove_vision_overlay(page)
        viewport = await page.evaluate("() => ({width: innerWidth, height: innerHeight})")
        client = ollama.AsyncClient(host=config.OLLAMA_HOST)
        await _unload_model(client, config.MODEL)
        prompt = (
            "Do not explain or think. Immediately output one JSON object with integer x, integer y, "
            "string label, number confidence (0..1), string reason, and string element_id. "
            "Locate the requested UI target in this browser screenshot. Red boxes are DOM controls "
            "and their badges are element IDs; return that element_id when the correct target is "
            "marked, otherwise return an empty string. Coordinates must be viewport pixels. "
            f"Viewport: {viewport['width']}x{viewport['height']}. Target: {description}. "
            f"Visible DOM candidates: {json.dumps(candidates[:50], ensure_ascii=False)}"
        )
        try:
            response = await client.chat(
                model=config.VISION_MODEL,
                messages=[{"role": "user", "content": prompt, "images": [str(path)]}],
                format="json",
                think=False,
                options={"temperature": 0, "num_predict": 1024, "num_ctx": config.NUM_CTX},
                keep_alive=0,
            )
            content = response["message"]["content"]
            target = json.loads(content)
        finally:
            path.unlink(missing_ok=True)
            await _unload_model(client, config.VISION_MODEL)
            try:
                await client.chat(
                    model=config.MODEL,
                    messages=[{"role": "user", "content": "Say ready"}],
                    options={"num_ctx": config.NUM_CTX},
                    keep_alive=config.KEEP_ALIVE,
                )
            except Exception:
                pass
        element_id = str(target.get("element_id", "")).strip()
        raw_x = target.get("x")
        if not element_id and isinstance(raw_x, str) and raw_x.strip() in _last:
            element_id = raw_x.strip()
        if element_id not in _last:
            element_id = ""
        if element_id:
            el = _last[element_id]
            box = await _frame_for(el).locator(
                f'[data-agent-id="{element_id}"]').bounding_box(timeout=15000)
            if box is None:
                return "Error: the VLM-selected DOM element is no longer visible."
            x = int(box["x"] + box["width"] / 2)
            y = int(box["y"] + box["height"] / 2)
        else:
            x, y = int(target["x"]), int(target["y"])
        confidence = float(target.get("confidence", 0))
        if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
            return "Error: vision returned coordinates outside the viewport."
        if confidence < config.VISION_MIN_CONFIDENCE:
            return f"Error: visual target confidence {confidence:.2f} is too low."
        _visual_target = {
            "x": x, "y": y, "label": str(target.get("label", description)),
            "confidence": confidence, "description": description, "element_id": element_id,
        }
        _audit("visual_inspect", _visual_target, ok=True)
        return json.dumps(_visual_target, ensure_ascii=False)
    except Exception as e:
        _visual_target = None
        return f"Error: visual inspection failed: {e}"


async def visual_click(args: dict) -> str:
    global _visual_target
    try:
        page = await _ctx()
        if await _challenge_on(page):
            return CHALLENGE_MSG
        target = _visual_target
        if target is None:
            return "Error: call visual_inspect first."
        if target.get("element_id") in _last:
            field_id = target["element_id"]
            result = await click_element(
                {"field_id": field_id, "_no_visual": bool(args.get("_no_visual"))})
            if not result.startswith(("Error", "User DENIED")):
                _audit("visual_dom_click", {**target, "field": field_id}, ok=True)
                _visual_target = None
            return result
        hit = await page.evaluate(
            """({x,y}) => {
              const el = document.elementFromPoint(x,y);
              if (!el) return null;
              const tag = el.tagName.toLowerCase();
              return {tag, type:(el.getAttribute('type')||'').toLowerCase(),
                text:(el.innerText||el.textContent||'').trim().slice(0,120),
                aria_label:el.getAttribute('aria-label')||'', in_form:!!el.closest('form'),
                opaque:['canvas','svg','html','body'].includes(tag)};
            }""",
            {"x": target["x"], "y": target["y"]},
        )
        opaque = hit is None or hit.get("opaque") or not (hit.get("text") or hit.get("aria_label"))
        if opaque or gate.is_irreversible_click(hit):
            summary = (
                f"Visual click '{target['label']}' on {page.url} at "
                f"({target['x']}, {target['y']}). "
                + ("The target has no reliable DOM semantics." if opaque else "The target may be consequential.")
            )
            if not await _confirm_cb(summary):
                return DENIED_MSG
        await page.mouse.click(target["x"], target["y"])
        _audit("visual_click", {**target, "opaque": opaque}, ok=True)
        _visual_target = None
        return await _fresh_digest()
    except Exception as e:
        return f"Error: visual click failed: {e}"


# --- tool registry ---
TOOLS = {
    "browser_goto": {
        "schema": {
            "type": "function",
            "function": {
                "name": "browser_goto",
                "description": "Open a URL in the browser. Returns a digest of the page's interactive elements (inputs, selects, buttons, links) with ids usable by fill_field/click_element.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Absolute URL to open, e.g. https://example.com"}
                    },
                    "required": ["url"],
                },
            },
        },
        "fn": browser_goto,
    },
    "read_page": {
        "schema": {
            "type": "function",
            "function": {
                "name": "read_page",
                "description": "Read the current page's title and visible text (trimmed).",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        "fn": read_page,
    },
    "extract_form_fields": {
        "schema": {
            "type": "function",
            "function": {
                "name": "extract_form_fields",
                "description": "Re-scan the current page and return a fresh digest of interactive elements with their ids, labels and current values.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        "fn": extract_form_fields,
    },
    "fill_field": {
        "schema": {
            "type": "function",
            "function": {
                "name": "fill_field",
                "description": "Type a value into a text input/textarea, or check/uncheck a checkbox (value true/false). Use the element id from the latest digest.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string", "description": "Element id from the digest, e.g. e3"},
                        "value": {"type": "string", "description": "Text to fill, or true/false for checkboxes"},
                    },
                    "required": ["field_id", "value"],
                },
            },
        },
        "fn": fill_field,
    },
    "upload_file": {
        "schema": {
            "type": "function",
            "function": {
                "name": "upload_file",
                "description": "Attach a local file (e.g. resume PDF) to a file-upload field on the page. Use the element id of a 'file input' from the digest.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string", "description": "Element id of the file input, e.g. e7"},
                        "path": {"type": "string", "description": "Absolute path of the file to attach"},
                    },
                    "required": ["field_id", "path"],
                },
            },
        },
        "fn": upload_file,
    },
    "select_option": {
        "schema": {
            "type": "function",
            "function": {
                "name": "select_option",
                "description": "Choose an option in a <select> dropdown by its visible label (falls back to value).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string", "description": "Element id from the digest, e.g. e5"},
                        "option": {"type": "string", "description": "Option label (or value) to select"},
                    },
                    "required": ["field_id", "option"],
                },
            },
        },
        "fn": select_option,
    },
    "click_element": {
        "schema": {
            "type": "function",
            "function": {
                "name": "click_element",
                "description": "Click a button/link by its element id. Submit-like clicks require human approval and may be denied — never retry a denied click.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string", "description": "Element id from the digest, e.g. e8"}
                    },
                    "required": ["field_id"],
                },
            },
        },
        "fn": click_element,
    },
    "screenshot_page": {
        "schema": {
            "type": "function",
            "function": {
                "name": "screenshot_page",
                "description": "Save a screenshot of the current browser page to the screenshots folder.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        "fn": screenshot_page,
    },
    "list_tabs": {
        "schema": {
            "type": "function",
            "function": {
                "name": "list_tabs",
                "description": "List the open browser tabs (index, title, url). Use to find the tab the user is looking at.",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        "fn": list_tabs,
    },
    "switch_tab": {
        "schema": {
            "type": "function",
            "function": {
                "name": "switch_tab",
                "description": "Switch to an open tab by its index (from list_tabs) and read it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer", "description": "Tab index from list_tabs, e.g. 0"}
                    },
                    "required": ["index"],
                },
            },
        },
        "fn": switch_tab,
    },
    "new_tab": {
        "schema": {
            "type": "function",
            "function": {
                "name": "new_tab",
                "description": "Open a new browser tab, optionally navigating to a URL, and read it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "Optional URL to open in the new tab"}
                    },
                    "required": [],
                },
            },
        },
        "fn": new_tab,
    },
}


def _simple_tool(name: str, description: str, properties=None, required=None, fn=None) -> dict:
    return {
        "schema": {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": properties or {},
                    "required": required or [],
                },
            },
        },
        "fn": fn,
    }


TOOLS.update({
    "browser_back": _simple_tool("browser_back", "Go back in the current tab and read it.", fn=browser_back),
    "browser_forward": _simple_tool("browser_forward", "Go forward in the current tab and read it.", fn=browser_forward),
    "reload_page": _simple_tool("reload_page", "Reload the current page and read it.", fn=reload_page),
    "scroll_page": _simple_tool(
        "scroll_page", "Scroll vertically; use a negative amount to scroll up.",
        {"amount": {"type": "integer", "description": "Pixels, from -4000 to 4000"}},
        ["amount"], scroll_page),
    "press_key": _simple_tool(
        "press_key", "Press a browser keyboard key such as Enter, Escape, Tab, or ArrowDown.",
        {"key": {"type": "string"}}, ["key"], press_key),
    "hover_element": _simple_tool(
        "hover_element", "Hover over an element from the latest digest.",
        {"field_id": {"type": "string"}}, ["field_id"], hover_element),
    "scroll_to_element": _simple_tool(
        "scroll_to_element", "Bring an extracted element into the visible viewport.",
        {"field_id": {"type": "string"}}, ["field_id"], scroll_to_element),
    "drag_and_drop": _simple_tool(
        "drag_and_drop", "Drag one extracted element onto another.",
        {"source_id": {"type": "string"}, "target_id": {"type": "string"}},
        ["source_id", "target_id"], drag_and_drop),
    "close_tab": _simple_tool(
        "close_tab", "Close a tab Jerry opened. Existing user tabs cannot be closed.",
        {"index": {"type": "integer"}}, ["index"], close_tab),
    "prepare_dialog": _simple_tool(
        "prepare_dialog", "Choose how to handle the next JavaScript alert/confirm/prompt before clicking its trigger.",
        {"action": {"type": "string", "enum": ["accept", "dismiss"]},
         "text": {"type": "string", "description": "Optional prompt response"}},
        ["action"], prepare_dialog),
    "download_element": _simple_tool(
        "download_element", "Click an extracted download control and save the file as a Telegram artifact.",
        {"field_id": {"type": "string"}}, ["field_id"], download_element),
    "fill_secret_field": _simple_tool(
        "fill_secret_field", "Fill a password or OTP field using the opaque handle from request_secret.",
        {"field_id": {"type": "string"}, "secret_handle": {"type": "string"}},
        ["field_id", "secret_handle"], fill_secret_field),
    "scrape_page": _simple_tool(
        "scrape_page", "Extract a sourced summary, links, or structured rows from the current page. Structured results are attached to Telegram as CSV.",
        {"mode": {"type": "string", "enum": ["summary", "links", "rows"]},
         "selector": {"type": "string", "description": "Optional CSS selector narrowing the extraction"},
         "format": {"type": "string", "enum": ["auto", "csv", "json"],
                    "description": "auto uses CSV for rows and JSON for nested data"},
         "max_items": {"type": "integer", "description": "Maximum 200"}},
        ["mode"], scrape_page),
    "visual_inspect": _simple_tool(
        "visual_inspect", "Use the local vision model to locate a visual-only control. Never use on CAPTCHA or verification challenges.",
        {"target": {"type": "string", "description": "Precise description of the control"}},
        ["target"], visual_inspect),
    "visual_click": _simple_tool(
        "visual_click", "Click the validated target from visual_inspect. Opaque targets require Telegram approval.",
        {}, [], visual_click),
    "find_elements": _simple_tool(
        "find_elements", "Search every extracted control by label/text/name, including controls omitted from a truncated digest.",
        {"query": {"type": "string"},
         "group": {"type": "string", "description": "Optional exact radio/input group name"}},
        [], find_elements),
    "choose_option": _simple_tool(
        "choose_option", "Select a radio/checkbox choice by semantic label and optional group name, then verify it is checked. Returns available choices instead of guessing when absent or ambiguous.",
        {"label": {"type": "string"}, "group": {"type": "string"}},
        ["label"], choose_option),
})
