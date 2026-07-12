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
from urllib.parse import urlencode, urlsplit

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
_known: dict[str, dict] = {}  # prior semantic fingerprints for rerender recovery
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
    global _confirm_cb, _task, _secret_resolver, _visual_target, _visual_attempts, _scroll_streak
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
    _scroll_streak = 0
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
        token_page = None
        wanted_token = (_task.source_tab_token if _task is not None else None) or ""
        if wanted_token:
            for candidate in _context.pages:
                try:
                    marker = await candidate.evaluate(
                        "() => document.documentElement?.dataset?.jerryTabToken || ''")
                    if marker == wanted_token:
                        token_page = candidate
                        break
                except Exception:
                    continue
        exact = next((p for p in _context.pages
                      if (p.url or "").rstrip("/") == wanted.rstrip("/")), None)
        prefix = next((p for p in _context.pages
                       if wanted and (p.url or "").startswith(wanted.split("#", 1)[0])), None)
        _page = token_page or exact or prefix or _context.pages[-1]
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
    _known.clear()
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
  const cleanText = (value) => String(value || '').trim().replace(/\\s+/g, ' ');
  const textOf = (el, limit = 80) => cleanText(el && (el.innerText || el.textContent || '')).slice(0, limit);
  const labelTextOf = (el, limit = 180) => {
    if (!el) return '';
    const clone = el.cloneNode(true);
    clone.querySelectorAll('input, select, textarea, button, option, script, style')
      .forEach((node) => node.remove());
    return textOf(clone, limit);
  };

  const cand = new Set();
  document.querySelectorAll(
    'input:not([type=hidden]), select, textarea, button, a[href], [role=button], '
    + '[role=checkbox], [role=radio], [role=combobox], [role=option], [aria-checked], [onclick]'
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
      if (l && labelTextOf(l, 80)) return labelTextOf(l, 80);
    }
    const aria = el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim();
    const ph = el.getAttribute('placeholder');
    if (ph && ph.trim()) return ph.trim();
    const wrap = el.closest('label');
    if (wrap && labelTextOf(wrap, 80)) return labelTextOf(wrap, 80);
    // Lever and similar ATS pages put the question beside the field rather than
    // associating it through standard label/aria attributes.
    const appQuestion = el.closest('.application-question');
    if (appQuestion) {
      const appLabel = appQuestion.querySelector(
        '.application-label .text, .application-label, :scope > label, :scope > div > label');
      if (appLabel && labelTextOf(appLabel, 80)) return labelTextOf(appLabel, 80);
    }
    if (el.tagName === 'BUTTON' || el.tagName === 'A' || el.getAttribute('role') === 'button'
        || el.tagName === 'DIV' || el.tagName === 'SPAN') {
      if (textOf(el)) return textOf(el);
    }
    const sib = el.previousElementSibling;
    if (isFormField(el) && sib && sib.tagName === 'LABEL' && labelTextOf(sib, 80)) {
      return labelTextOf(sib, 80);
    }
    const nm = el.getAttribute('name');
    if (nm) return nm;
    return '';
  };

  const questionOf = (el) => {
    const fieldset = el.closest('fieldset');
    if (fieldset) {
      const legend = fieldset.querySelector(':scope > legend');
      if (legend && textOf(legend)) return textOf(legend);
    }
    const group = el.closest('[role=radiogroup], [role=group]');
    if (group) {
      const aria = group.getAttribute('aria-label');
      if (aria) return aria.trim();
      const labelledBy = group.getAttribute('aria-labelledby');
      if (labelledBy) {
        const label = document.getElementById(labelledBy);
        if (label && textOf(label)) return textOf(label);
      }
    }
    const appQuestion = el.closest('.application-question');
    if (appQuestion) {
      const appLabel = appQuestion.querySelector(
        '.application-label .text, .application-label, :scope > label, :scope > div > label');
      const text = labelTextOf(appLabel, 180);
      if (text) return text;
    }
    const semanticQuestion = el.closest(
      '[data-question], [data-testid*="question"], [data-qa*="question"], '
      + '.form-question, .form-field, .field-wrapper');
    if (semanticQuestion) {
      const heading = semanticQuestion.querySelector(
        'legend, [class*="label"], [data-testid*="label"], [data-qa*="label"]');
      const text = labelTextOf(heading, 180);
      if (text) return text;
    }
    return labelOf(el);
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
    const role = (el.getAttribute('role') || '').toLowerCase();
    const type = (el.getAttribute('type')
      || (role === 'checkbox' || role === 'radio' || role === 'combobox' ? role : '')
      || (tag === 'input' ? 'text' : '')).toLowerCase();
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
      checked: (type === 'checkbox' || type === 'radio')
        ? (('checked' in el) ? !!el.checked : el.getAttribute('aria-checked') === 'true')
        : null,
      options: options,
      in_form: el.closest('form') !== null,
      required: !!el.required || el.getAttribute('aria-required') === 'true',
      autocomplete: el.getAttribute('autocomplete') || '',
      role: role,
      question: questionOf(el).slice(0, 180),
      scope: el.closest('dialog[open], [role=dialog]') ? 'dialog' : 'page',
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
            _known[el["id"]] = dict(el)
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
        question = el.get("question") or ""
        distinct_question = question if question.lower() != label.lower() else ""
        if typ in ("checkbox", "radio"):
            line = f'[{eid}] {typ} "{label}" {"checked" if el.get("checked") else "unchecked"}'
            if distinct_question:
                line += f' — question: "{distinct_question}"'
        elif tag == "input":
            line = f'[{eid}] {typ or "text"} input "{question or label}" value="{el.get("value", "")}"'
        elif tag == "select":
            line = f'[{eid}] select "{question or label}" options: {el.get("options", "")} value="{el.get("value", "")}"'
        elif tag == "textarea":
            line = f'[{eid}] textarea "{question or label}" value="{el.get("value", "")}"'
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
                # Many application sites preload an invisible reCAPTCHA frame even
                # while the form is fully usable. Stop only when a challenge widget
                # is actually presented at a meaningful visible size.
                try:
                    owner = await f.frame_element()
                    box = await owner.bounding_box()
                    if (await owner.is_visible() and box
                            and box["width"] >= 100 and box["height"] >= 35):
                        return True
                except Exception:
                    pass
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


async def _resolve_element(field_id: str):
    """Resolve a stale digest id after a React rerender using semantic attributes."""
    el = _last.get(field_id)
    if el is not None:
        return field_id, el
    old = _known.get(field_id)
    if old is None:
        return field_id, None
    await _extract()
    for new_id, candidate in _last.items():
        if any(old.get(k) and old.get(k) == candidate.get(k)
               for k in ("dom_id", "data_id")):
            return new_id, candidate
    keys = ("tag", "type", "name", "value", "label")
    fingerprint = tuple(str(old.get(k) or "").lower() for k in keys)
    matches = [item for item in _last.items()
               if tuple(str(item[1].get(k) or "").lower() for k in keys) == fingerprint]
    return matches[0] if len(matches) == 1 else (field_id, None)


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
        # Many real ATS forms (including Lever) wrap an id-less native input in
        # a visible label. Clicking the hidden input itself is slow or rejected.
        wrap = frame.locator(f'[data-agent-id="{field_id}"]').locator("xpath=ancestor::label[1]")
        if await wrap.count() == 1 and await wrap.is_visible():
            return wrap, loc
    return loc, loc


async def _set_choice_state(frame, el: dict, field_id: str, desired: bool) -> None:
    """Set a checkbox/radio state across native, wrapped, and React-controlled forms."""
    clickable, native = await _clickable_control(frame, el, field_id)
    if await native.is_checked() == desired:
        return
    if (el.get("tag") or "").lower() != "input":
        try:
            await clickable.press("Space", timeout=3000)
        except Exception:
            pass
        if page is not None:
            await page.wait_for_timeout(150)
        if await native.is_checked() == desired:
            return
        raise RuntimeError(
            f"{el.get('label') or field_id} did not reach "
            f"{'checked' if desired else 'unchecked'} state")
    try:
        await clickable.click(timeout=6000)
    except Exception:
        pass
    page = page_or_none()
    if page is not None:
        await page.wait_for_timeout(150)
    if await native.is_checked() == desired:
        return
    try:
        if desired:
            await native.check(force=True, timeout=4000)
        else:
            await native.uncheck(force=True, timeout=4000)
    except Exception:
        pass
    if page is not None:
        await page.wait_for_timeout(150)
    if await native.is_checked() == desired:
        return
    # Some controlled components cancel synthetic pointer clicks after another
    # field rerenders. Use the browser's native property setter and emit the same
    # bubbling events a human click produces, then verify rather than assuming.
    await native.evaluate(
        """(el, desired) => {
          const win = el.ownerDocument.defaultView;
          const setter = Object.getOwnPropertyDescriptor(
            win.HTMLInputElement.prototype, 'checked').set;
          setter.call(el, desired);
          el.dispatchEvent(new win.Event('input', {bubbles: true}));
          el.dispatchEvent(new win.Event('change', {bubbles: true}));
        }""", desired)
    if page is not None:
        await page.wait_for_timeout(200)
    if await native.is_checked() != desired:
        raise RuntimeError(
            f"{el.get('label') or field_id} did not reach "
            f"{'checked' if desired else 'unchecked'} state")


_FALSEY = {"false", "0", "no", "off", "unchecked", "none", ""}
_FACTUAL_FIELD = re.compile(
    r"name|email|phone|address|location|city|state|country|nationality|zip|postal|school|university|college|"
    r"degree|graduat|company|employer|linkedin|github|portfolio|website|salary|visa|"
    r"sponsor|authoriz|citizen|race|ethnic|hispanic|disab|veteran|gender|pronoun",
    re.I,
)


def _semantic_norm(value: str) -> str:
    """Normalize visible form text without losing meaningful words/numbers."""
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def _semantic_query_matches(query: str, candidate: str) -> bool:
    wanted = _semantic_norm(query)
    actual = _semantic_norm(candidate)
    return bool(wanted and actual and (wanted == actual or wanted in actual))


def _answer_matches_option(answer: str, option: str) -> bool:
    """Exact match plus safe label expansion, e.g. English -> English (ENG)."""
    wanted = _semantic_norm(answer)
    actual = _semantic_norm(option)
    return bool(wanted and actual and (
        wanted == actual
        or actual.startswith(wanted + " ")
        or wanted.startswith(actual + " ")
    ))


def _flatten_scalars(value) -> list[str]:
    if isinstance(value, dict):
        return [x for v in value.values() for x in _flatten_scalars(v)]
    if isinstance(value, list):
        return [x for v in value for x in _flatten_scalars(v)]
    text = str(value or "").strip()
    return [text] if text and not text.startswith("(unset") else []


async def _grounded_value(value: str, *, choice: bool = False) -> bool:
    """Accept factual application values only from profile or the current request."""
    import tools_fs
    import yaml
    wanted = str(value or "").strip().lower()
    if not wanted:
        return True
    profile_text = await tools_fs.read_profile({})
    try:
        scalars = [x.lower() for x in _flatten_scalars(yaml.safe_load(profile_text) or {})]
    except Exception:
        scalars = []
    request = ((_task.text if _task else "") + " " +
               ((_task.source_url or "") if _task else "")).lower()
    if wanted in scalars:
        if choice and wanted in {"yes", "no", "none", "other"}:
            return f"choose {wanted}" in request or f"answer {wanted}" in request
        return True
    if len(wanted) >= 2 and any(
            wanted in re.findall(r"[a-z0-9@.+-]+", scalar) for scalar in scalars):
        return True
    if len(wanted) >= 4 and wanted in request:
        return True
    if choice:
        meaningful = [t for t in re.findall(r"[a-z0-9]+", wanted)
                      if len(t) >= 3 and t not in {"the", "and", "from", "with", "option"}]
        grounded_text = " ".join(scalars + [request])
        return any(re.search(rf"\b{re.escape(token)}\b", grounded_text) for token in meaningful)
    return False


async def _grounded_form_choice(value: str, question: str, *, choice: bool) -> bool:
    """Ground a choice against the matching question, not an unrelated profile token."""
    import tools_fs
    import profile_store

    value = str(value or "").strip()
    question = str(question or "").strip()
    if not value:
        return True
    request = ((_task.text if _task else "") + " " +
               ((_task.source_url or "") if _task else "")).lower()
    wanted_in_request = _semantic_norm(value)
    normalized_request = _semantic_norm(request)
    if (len(wanted_in_request) >= 2
            and re.search(rf"(?<![a-z0-9]){re.escape(wanted_in_request)}(?![a-z0-9])",
                          normalized_request)):
        return True
    remembered = profile_store.application_answer(question)
    if remembered and _answer_matches_option(remembered, value):
        return True
    profile = tools_fs.load_profile_data()
    for answer in _profile_choice_values_for_question(profile, question):
        if _answer_matches_option(answer, value):
            return True
    mapped = _profile_value_for_field(profile, {"question": question})
    if mapped and _answer_matches_option(mapped, value):
        return True
    # Non-generic choices such as a language or office location can still be
    # grounded in a profile scalar. Generic Yes/No must be tied to this question.
    if _semantic_norm(value) not in {"yes", "no", "none", "other"}:
        return await _grounded_value(value, choice=choice)
    return False


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
                                ("question", "label", "text", "aria_label", "name", "value")).lower()
            if query and query not in haystack:
                continue
            if group and group not in str(el.get("name") or "").lower():
                continue
            matches.append({
                "field_id": field_id,
                "tag": el.get("tag"),
                "type": el.get("type"),
                "group": el.get("name"),
                "question": el.get("question"),
                "label": el.get("label") or el.get("aria_label") or el.get("text"),
                "value": el.get("value"),
                "checked": el.get("checked"),
            })
        _audit("find_elements", {"query": query, "group": group,
                                 "matches": len(matches)}, ok=True)
        if not matches:
            application_form = (
                bool(re.search(r"/apply|/application|jobs?\.", page.url, re.I))
                or (any(el.get("type") == "file" for el in _last.values())
                    and any(el.get("tag") == "textarea" for el in _last.values()))
            )
            if application_form:
                return (f"No application-form control matched '{query or group}'. "
                        "Do not invent this question and do not use vision to click a "
                        "different field. Re-read extract_form_fields and work only "
                        "from the questions actually returned by the page.")
            return (f"No DOM controls matched '{query or group}'. On app-style sites many "
                    "controls (color swatches, icon buttons, image pickers, sliders) carry "
                    "no text in the DOM — use visual_inspect with a plain description "
                    f"(e.g. visual_inspect(target='the {query or group} option')) instead "
                    "of retrying text searches.")
        return json.dumps(matches[:30], ensure_ascii=False)
    except Exception as e:
        return f"Error: element search failed: {e}"


async def choose_option(args: dict) -> str:
    """Choose a radio/checkbox/button by semantic label rather than a transient element id."""
    global _scroll_streak
    _scroll_streak = 0
    try:
        page = await _ctx()
        await _settle(page)
        if await _challenge_on(page):
            return CHALLENGE_MSG
        await _extract()
        label = str(args.get("label", "")).strip()
        group = str(args.get("group", "")).strip()
        question = str(args.get("question", "")).strip()
        raw_selected = args.get("selected", True)
        selected_state = (raw_selected if isinstance(raw_selected, bool)
                          else str(raw_selected).strip().lower() not in _FALSEY)
        if not label:
            return "Error: label is required"
        eligible = []
        group_choices = []
        for field_id, el in _last.items():
            if (el.get("type") or "").lower() not in ("radio", "checkbox"):
                continue
            if group and str(el.get("name") or "").lower() != group.lower():
                continue
            actual_question = str(el.get("question") or "")
            if question and not _semantic_query_matches(question, actual_question):
                continue
            display = str(el.get("label") or el.get("aria_label") or el.get("value") or "")
            group_choices.append(
                f"{actual_question}: {display}" if actual_question and actual_question != display
                else display)
            if _semantic_norm(display) == _semantic_norm(label):
                eligible.insert(0, (field_id, el, True))
            elif _semantic_query_matches(label, display):
                eligible.append((field_id, el, False))
        exact = [x for x in eligible if x[2]]
        selected = exact or eligible
        if len(selected) != 1:
            reason = "no exact choice" if not selected else "ambiguous choice"
            scope = question or group or "(any question)"
            return (f"Error: {reason} for {label!r} under {scope!r}. "
                    f"Available choices: {'; '.join(x for x in group_choices if x) or '(none)'}. "
                    "Provide the exact question text when labels such as Yes/No repeat.")
        field_id, el, _ = selected[0]
        if not selected_state:
            if (el.get("type") or "").lower() != "checkbox":
                return "Error: a radio option cannot be deselected directly; select the intended alternative."
            return await fill_field({"field_id": field_id, "value": False})
        if el.get("checked"):
            return f"Already selected: {el.get('label') or label}."
        return await click_element({"field_id": field_id})
    except Exception as e:
        return f"Error: could not choose option: {e}"


async def fill_field(args: dict) -> str:
    field_id = str(args.get("field_id") or "").strip()
    value = args.get("value", "")
    field_id, el = await _resolve_element(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    semantics = " ".join(str(el.get(k) or "") for k in
                         ("label", "aria_label", "text")).strip()
    if not semantics and not args.get("_from_visual"):
        return (f"Error: {field_id} is an unlabeled control and cannot be clicked directly. "
                "Use visual_inspect with the requested target, then visual_click.")
    label = str(el.get("label") or el.get("aria_label") or el.get("name") or "")
    narrative_field = (el.get("tag") == "textarea" and len(str(value).strip()) >= 80)
    if (str(value).strip() and _FACTUAL_FIELD.search(label) and not narrative_field
            and not await _grounded_value(str(value))):
        return (f"Error: {value!r} is not a verified profile value for {label!r}. "
                "Use ask_user instead of inventing application data.")
    global _scroll_streak
    _scroll_streak = 0
    try:
        frame = _frame_for(el)
        loc = _control_locator(frame, el, field_id)
        if (el.get("type") or "").lower() in ("checkbox", "radio"):
            desired = str(value).strip().lower() not in _FALSEY
            await _set_choice_state(frame, el, field_id, desired)
        else:
            await loc.fill(str(value), timeout=15000)
        _audit("fill", {"field": field_id, "label": el.get("label"), "value": value}, ok=True)
        return f"Filled {field_id}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not fill {field_id}: {e}"


async def select_option(args: dict) -> str:
    field_id = str(args.get("field_id") or "").strip()
    option = str(args.get("option", ""))
    question = str(args.get("question") or "").strip()
    if not field_id:
        if not question:
            return "Error: provide field_id or question"
        try:
            await _extract()
        except Exception as e:
            return f"Error: could not inspect dropdowns: {e}"
        candidates = [
            (candidate_id, candidate)
            for candidate_id, candidate in _last.items()
            if candidate.get("tag") == "select"
            and _semantic_query_matches(
                question, candidate.get("question") or candidate.get("label") or "")
        ]
        if len(candidates) != 1:
            available = [
                candidate.get("question") or candidate.get("label") or candidate_id
                for candidate_id, candidate in _last.items()
                if candidate.get("tag") == "select"
            ]
            reason = "not found" if not candidates else "ambiguous"
            return (f"Error: dropdown question {question!r} was {reason}. "
                    f"Available dropdowns: {'; '.join(available[:25]) or '(none)'}")
        field_id = candidates[0][0]
    field_id, el = await _resolve_element(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    if el.get("tag") != "select":
        return f"Error: field {field_id} is not a native select dropdown."
    global _scroll_streak
    _scroll_streak = 0
    try:
        frame = _frame_for(el)
        loc = _control_locator(frame, el, field_id)
        choices = await loc.locator("option").evaluate_all(
            """options => options.map((o, index) => ({
              index, label: String(o.label || o.textContent || '').trim(),
              value: String(o.value || '')
            }))""")
        wanted = _semantic_norm(option)
        exact = [
            choice for choice in choices
            if _semantic_norm(choice["label"]) == wanted
            or _semantic_norm(choice["value"]) == wanted
        ]
        semantic_label = str(el.get("question") or el.get("label") or "")
        if len(exact) != 1:
            return (f"Error: exact dropdown option {option!r} was not found in "
                    f"{semantic_label or field_id!r}. Available options: "
                    f"{'; '.join(x['label'] for x in choices[:40])}")
        if (_FACTUAL_FIELD.search(semantic_label)
                and not await _grounded_form_choice(option, semantic_label, choice=False)):
            return (f"Error: option {option!r} is not a verified profile value for "
                    f"{semantic_label!r}. Use ask_user instead of guessing.")
        await loc.select_option(index=exact[0]["index"], timeout=15000)
        selected_label = await loc.locator("option:checked").first.inner_text()
        if _semantic_norm(selected_label) != _semantic_norm(exact[0]["label"]):
            raise RuntimeError(f"dropdown reported {selected_label!r} after selection")
        _audit("select", {"field": field_id, "label": semantic_label,
                          "option": exact[0]["label"]}, ok=True)
        return (f"Selected \"{exact[0]['label']}\" for \"{semantic_label or field_id}\". "
                "Verified the dropdown value.\n" + await _fresh_digest())
    except Exception as e:
        return f"Error: could not select \"{option}\" in {field_id}: {e}"


async def upload_file(args: dict) -> str:
    field_id = str(args.get("field_id") or "").strip()
    path = str(args.get("path") or "")
    field_id, el = await _resolve_element(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        import tools_fs
        p = tools_fs._safe(path, allow_artifacts=True)
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
    field_id, el = await _resolve_element(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    if (el.get("type") or "").lower() in ("radio", "checkbox"):
        choice = str(el.get("label") or el.get("aria_label") or el.get("value") or "")
        question = str(el.get("question") or "")
        if (not el.get("checked")
                and not await _grounded_form_choice(choice, question, choice=True)):
            return (f"Error: option {choice!r} is not grounded in the user's profile or request. "
                    "Use ask_user instead of guessing this application answer.")
    global _scroll_streak
    _scroll_streak = 0
    try:
        page = await _ctx()
        frame = _frame_for(el)
        loc, native = await _clickable_control(frame, el, field_id)
        if (el.get("type") or "").lower() in ("radio", "checkbox") and await native.is_checked():
            return (f"Already selected: {el.get('label') or field_id}. "
                    "Verified the control is checked.\n" + await _fresh_digest())
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
        application_entry = gate.is_application_entry_click(el)
        if gate.is_irreversible_click(el) and not application_entry and not login_authorized:
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
        if _dialog_action is not None:
            page.once("dialog", _handle_next_dialog)
        before_click = await _page_action_fingerprint(page)
        post_state_verified = False
        if (el.get("type") or "").lower() in ("radio", "checkbox"):
            await _set_choice_state(frame, el, field_id, True)
        else:
            try:
                await loc.click(timeout=7000)
            except Exception as click_error:
                # Some React controls replace or cover themselves after receiving
                # the click, causing Playwright to time out even though the page
                # advanced. Accept only a verified URL/dialog/DOM state change.
                await page.wait_for_timeout(300)
                after_click = await _page_action_fingerprint(page)
                if after_click == before_click:
                    raise click_error
                post_state_verified = True
        verified = ""
        if (el.get("type") or "").lower() in ("radio", "checkbox"):
            await page.wait_for_timeout(300)
            refreshed = _control_locator(frame, el, field_id)
            if not await refreshed.is_checked():
                raise RuntimeError(f"{el.get('label') or field_id} was clicked but is not selected")
            verified = f" Verified selected: {el.get('label') or field_id}."
        _dom_failures.pop(field_id, None)
        _audit("application_open" if application_entry else "click",
               {"field": field_id, "label": el.get("label")}, ok=True)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeoutError:
            pass
        if post_state_verified:
            verified += " Verified the page changed after the click."
        return f"Clicked {field_id}.{verified}\n" + await _fresh_digest()
    except Exception as e:
        _dom_failures[field_id] = _dom_failures.get(field_id, 0) + 1
        if ((el.get("type") or "").lower() not in ("radio", "checkbox")
                and _dom_failures[field_id] >= 2 and config.VISION_ENABLED
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
        # Reloading a form destroys unsaved user input. Permit it only as a
        # recovery after Jerry has objective evidence that normal DOM actions are
        # repeatedly failing or that scrolling is no longer making progress.
        repeated_failures = max(_dom_failures.values(), default=0) >= 2
        navigation_stuck = _scroll_streak > 3
        if not (repeated_failures or navigation_stuck):
            return ("Error: reload refused because the page is not demonstrably stuck. "
                    "Reloading can erase completed form fields. Re-read the current page "
                    "with extract_form_fields/read_page and continue without refreshing.")
        page = await _ctx()
        await page.reload(wait_until="load", timeout=20000)
        _dom_failures.clear()
        _audit("reload", {"url": page.url}, ok=True)
        return await _fresh_digest()
    except Exception as e:
        return f"Error: could not reload: {e}"


_SCROLL_JS = """(amt) => {
  const visible = (el) => {
    if (el === document.scrollingElement) return true;
    const r = el.getBoundingClientRect(), s = getComputedStyle(el);
    return r.width >= 240 && r.height >= Math.min(220, innerHeight * .4) &&
      r.bottom > 0 && r.right > 0 && r.top < innerHeight && r.left < innerWidth &&
      s.visibility !== 'hidden' && s.display !== 'none' && Number(s.opacity || 1) > .05;
  };
  const candidates = [document.scrollingElement,
    ...Array.from(document.querySelectorAll('div,main,section,aside'))]
    .filter(Boolean)
    .filter(el => visible(el) && el.scrollHeight > el.clientHeight + 40 &&
      (el === document.scrollingElement || /(auto|scroll)/.test(getComputedStyle(el).overflowY)))
    .filter(el => !el.closest('nav,[role=listbox],[role=menu]'))
    .map(el => {
      const r = el === document.scrollingElement
        ? {left:0,top:0,width:innerWidth,height:innerHeight}
        : el.getBoundingClientRect();
      const range = el.scrollHeight - el.clientHeight;
      const room = amt >= 0 ? range - el.scrollTop : el.scrollTop;
      const rightPanel = r.left >= innerWidth * .35 ? r.width * r.height * .35 : 0;
      const mainBonus = /^(MAIN|ASIDE)$/.test(el.tagName) ? r.width * r.height * .3 : 0;
      return {el, score:r.width * r.height + rightPanel + mainBonus + Math.min(range, 4000) * 80,
        room, rect:r};
    })
    .filter(x => x.room > 2)
    .sort((a, b) => b.score - a.score);
  for (const candidate of candidates) {
    const el = candidate.el;
    const before = el.scrollTop;
    const oldBehavior = el.style.scrollBehavior;
    el.style.scrollBehavior = 'auto';
    el.scrollTop = before + amt;
    const after = el.scrollTop;
    el.style.scrollBehavior = oldBehavior;
    if (Math.abs(after - before) > 1)
      return {moved: Math.round(el.scrollTop - before),
              what: el === document.scrollingElement ? 'page'
                    : (el.tagName.toLowerCase() + '.' + String(el.className).split(' ')[0]).slice(0, 60),
              left:Math.round(candidate.rect.left), top:Math.round(candidate.rect.top),
              width:Math.round(candidate.rect.width), height:Math.round(candidate.rect.height)};
  }
  return null;
}"""


_scroll_streak = 0  # consecutive scrolls with no click/fill between them


async def scroll_page(args: dict) -> str:
    """Scroll the window — or, on app-style pages where the window is fixed (e.g. car
    configurators), the largest inner scrollable panel. Verifies something MOVED and
    says so, instead of reporting success while the screen sits still. Pacing up and
    down without acting is cut off mechanically: after 3 consecutive scrolls the tool
    refuses and points to visual_inspect."""
    global _scroll_streak
    try:
        _scroll_streak += 1
        if _scroll_streak > 3:
            return ("Error: you have scrolled repeatedly without clicking or filling "
                    "anything — scrolling more will not find it. The control you want "
                    "is probably visual-only. Call visual_inspect now with a plain "
                    "description of the target (e.g. 'the red paint swatch'), then "
                    "visual_click.")
        page = await _ctx()
        amount = max(-4000, min(4000, int(args.get("amount", 700))))
        res = None
        try:
            res = await page.evaluate(_SCROLL_JS, amount)
        except Exception:
            pass
        if res is None:  # nothing obviously scrollable: wheel at the viewport centre
            vp = await page.evaluate("() => ({w: innerWidth, h: innerHeight})")
            await page.mouse.move(vp["w"] * 0.75, vp["h"] / 2)
            await page.mouse.wheel(0, amount)
            note = ("(note: no scrollable area was detected — the wheel was tried at the "
                    "page centre; if content did not change, this page may not scroll and "
                    "you should use visual_inspect or click a section link instead)")
            _audit("scroll", {"amount": amount, "target": "wheel-fallback"}, ok=True)
        else:
            note = f"(scrolled {res['what']} by {res['moved']}px)"
            _audit("scroll", {"amount": amount, "target": res["what"],
                              "moved": res["moved"]}, ok=True)
        await page.wait_for_timeout(250)
        return f"{note}\n" + await _fresh_digest()
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


async def scrape_instagram_posts(args: dict) -> str:
    """Read visible Instagram posts/reels and retain their exact content URLs."""
    try:
        page = await _ctx()
        if "instagram.com" not in urlsplit(page.url).netloc.lower():
            instagram_page = next((candidate for candidate in (_context.pages if _context else [])
                                   if "instagram.com" in urlsplit(candidate.url).netloc.lower()), None)
            if instagram_page is None:
                return "Error: no open Instagram tab was found in the co-drive Edge session."
            await _select_page(instagram_page)
            page = instagram_page
        if await _challenge_on(page):
            return CHALLENGE_MSG
        max_posts = max(1, min(30, int(args.get("max_posts", 10))))
        scrolls = max(0, min(4, int(args.get("scrolls", 0))))
        found: dict[str, dict] = {}
        for step in range(scrolls + 1):
            batch = await page.evaluate(
                """(limit) => {
                  const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
                  const postPath = href => /^\/(p|reel|reels)\/[^/?#]+/.test(href || '');
                  const anchors = Array.from(document.querySelectorAll(
                    'a[href^="/p/"], a[href^="/reel/"], a[href^="/reels/"]'));
                  const out = [], seen = new Set();
                  for (const anchor of anchors) {
                    const href = anchor.getAttribute('href') || '';
                    if (!postPath(href)) continue;
                    const url = new URL(href, location.origin).href.split('?')[0];
                    if (seen.has(url)) continue;
                    seen.add(url);
                    const article = anchor.closest('article');
                    const container = article || anchor.closest('[role=button]') || anchor.parentElement;
                    const profileAnchors = article ? Array.from(article.querySelectorAll('a[href^="/"]')) : [];
                    const profile = profileAnchors.find(a => {
                      const p = (a.getAttribute('href') || '').split('?')[0];
                      return /^\/[A-Za-z0-9._]+\/$/.test(p) && !postPath(p);
                    });
                    const username = profile
                      ? (profile.getAttribute('href') || '').replace(/^\//,'').replace(/\/$/,'')
                      : (location.pathname.match(/^\/([A-Za-z0-9._]+)\/?$/) || [,''])[1];
                    const time = article && article.querySelector('time');
                    const image = (article || anchor).querySelector && (article || anchor).querySelector('img[alt]');
                    const text = clean(container && container.innerText).slice(0, 1600);
                    out.push({url, username, profile_url: username ? `${location.origin}/${username}/` : '',
                      timestamp: time ? (time.getAttribute('datetime') || clean(time.innerText)) : '',
                      media_description: image ? clean(image.getAttribute('alt')).slice(0, 600) : '',
                      visible_text: text,
                      sponsored: /\\bSponsored\\b/i.test(text)});
                    if (out.length >= limit) break;
                  }
                  return out;
                }""", max_posts)
            for post in batch:
                found.setdefault(post["url"], post)
            if len(found) >= max_posts or step >= scrolls:
                break
            await page.evaluate("() => window.scrollBy({top: Math.min(innerHeight * .8, 800), behavior: 'auto'})")
            await page.wait_for_timeout(800)
        posts = list(found.values())[:max_posts]
        if not posts:
            body = (await page.locator("body").inner_text(timeout=10000))[:1200]
            return ("No direct Instagram post/reel anchors are currently rendered. "
                    "The page may be on Stories, a private profile, or still loading. "
                    f"Visible text: {body}")
        for post in posts:
            _remember_source(post["url"])
        _audit("instagram_posts_scraped", {"count": len(posts), "scrolls": scrolls,
                                            "url": page.url}, ok=True)
        return json.dumps({"page": page.url, "count": len(posts), "posts": posts},
                          ensure_ascii=False)
    except Exception as e:
        return f"Error: Instagram post extraction failed: {e}"


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


async def _page_action_fingerprint(page) -> str:
    """Compact state used to prove that a visual click changed the rendered app."""
    data = await page.evaluate(
        """() => ({
          url: location.href,
          text: (document.body?.innerText || '').replace(/\s+/g,' ').slice(0,5000),
          selected: Array.from(document.querySelectorAll(
            'input:checked,[aria-checked=true],[aria-selected=true],[data-id-selected=true],.selected,.is-selected'))
            .slice(0,80).map(el => [el.id,el.getAttribute('data-id'),el.getAttribute('aria-label'),
              el.getAttribute('value'),String(el.className).slice(0,120)]),
          images: Array.from(document.images).slice(0,30).map(img => img.currentSrc || img.src),
          scroll: [scrollX,scrollY,...Array.from(document.querySelectorAll('div,main,section,aside'))
            .filter(el => el.scrollTop).slice(0,20).map(el => el.scrollTop)]
        })"""
    )
    return json.dumps(data, sort_keys=True, ensure_ascii=False)


async def visual_inspect(args: dict) -> str:
    global _visual_target, _visual_attempts
    if not config.VISION_ENABLED:
        return "Error: local vision is disabled."
    if _visual_attempts >= config.VISION_MAX_ATTEMPTS:
        return "Error: visual fallback failed twice consecutively; ask the user for help."
    try:
        page = await _ctx()
        if await _challenge_on(page):
            return CHALLENGE_MSG
        description = str(args.get("target", "")).strip()
        if not description:
            return "Error: describe the visual target"
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
            semantic = " ".join(str(el.get(k) or "") for k in
                                ("label", "aria_label", "text", "value")).lower()
            distinctive = [token for token in re.findall(r"[a-z0-9]+", description.lower())
                           if len(token) >= 3 and token not in {
                               "the", "and", "option", "button", "selected", "color",
                               "exterior", "interior", "target", "control"}]
            if semantic and distinctive and not any(token in semantic for token in distinctive):
                _visual_attempts += 1
                return ("Error: vision mapped the request to a contradictory DOM element "
                        f"({semantic[:100]!r}); scroll to the requested section and inspect again.")
            box = await _frame_for(el).locator(
                f'[data-agent-id="{element_id}"]').bounding_box(timeout=15000)
            if box is None:
                _visual_attempts += 1
                return "Error: the VLM-selected DOM element is no longer visible."
            x = int(box["x"] + box["width"] / 2)
            y = int(box["y"] + box["height"] / 2)
        else:
            x, y = int(target["x"]), int(target["y"])
        confidence = float(target.get("confidence", 0))
        if not (0 <= x < viewport["width"] and 0 <= y < viewport["height"]):
            _visual_attempts += 1
            return "Error: vision returned coordinates outside the viewport."
        if confidence < config.VISION_MIN_CONFIDENCE:
            _visual_attempts += 1
            return f"Error: visual target confidence {confidence:.2f} is too low."
        _visual_target = {
            "x": x, "y": y, "label": str(target.get("label", description)),
            "confidence": confidence, "description": description, "element_id": element_id,
        }
        _audit("visual_inspect", _visual_target, ok=True)
        _visual_attempts = 0
        return json.dumps(_visual_target, ensure_ascii=False)
    except Exception as e:
        _visual_target = None
        _visual_attempts += 1
        return f"Error: visual inspection failed: {e}"


async def visual_click(args: dict) -> str:
    global _visual_target
    global _scroll_streak
    _scroll_streak = 0
    try:
        page = await _ctx()
        if await _challenge_on(page):
            return CHALLENGE_MSG
        target = _visual_target
        if target is None:
            return "Error: call visual_inspect first."
        before = await _page_action_fingerprint(page)
        if target.get("element_id") in _last:
            field_id = target["element_id"]
            result = await click_element(
                {"field_id": field_id, "_no_visual": bool(args.get("_no_visual")),
                 "_from_visual": True})
            if not result.startswith(("Error", "User DENIED")):
                after = await _page_action_fingerprint(page)
                if after == before:
                    _audit("visual_dom_click_no_change", {**target, "field": field_id}, ok=False)
                    _visual_target = None
                    return ("Error: the visual click produced no detectable page or selection "
                            "change. Do not claim success; scroll/re-observe and target it again.")
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
        await page.wait_for_timeout(700)
        after = await _page_action_fingerprint(page)
        if after == before:
            _audit("visual_click_no_change", {**target, "opaque": opaque}, ok=False)
            _visual_target = None
            return ("Error: the visual click produced no detectable page or selection change. "
                    "Do not claim success; scroll/re-observe and target it again.")
        _audit("visual_click", {**target, "opaque": opaque}, ok=True)
        _visual_target = None
        return await _fresh_digest()
    except Exception as e:
        return f"Error: visual click failed: {e}"


# --- deterministic job search and application workflow ---

def _usable_profile_value(value) -> str:
    if isinstance(value, (dict, list)):
        return ""
    text = str(value or "").strip()
    return "" if (not text or text.lower().startswith("(unset")
                  or text.lower().startswith("(leave blank")) else text


def _profile_choice_values_for_question(profile: dict, question: str) -> list[str]:
    """Return only profile facts semantically tied to this choice question."""
    question = _semantic_norm(question)
    keys: list[str] = []
    if "language" in question:
        keys = ["languages", "language_skills"]
    elif "legally authorized" in question or "work authorization" in question:
        keys = ["work_authorization", "work_authorized", "authorized_to_work"]
    elif "sponsor" in question or "visa status" in question:
        keys = ["sponsorship", "requires_sponsorship", "visa_sponsorship", "visa_status"]
    elif "final internship" in question:
        keys = ["final_internship"]
    elif "offer deadline" in question:
        keys = ["offer_deadlines", "offer_deadline"]
    elif "preferred office" in question or "preferred location" in question:
        keys = ["preferred_office_locations", "preferred_locations"]
    elif "preferred palantir product" in question:
        keys = ["preferred_palantir_products"]
    elif "ai notetaker" in question or "transcribe conversations" in question:
        keys = ["ai_notetaker_consent"]
    elif "veteran" in question:
        keys = ["veteran_status"]
    elif "disab" in question:
        keys = ["disabilities", "disability_status"]
    elif "hispanic" in question or "latino" in question:
        keys = ["hispanic"]
    elif "race" in question or "ethnic" in question:
        keys = ["race"]
    elif "gender" in question:
        keys = ["gender"]

    values: list[str] = []
    for key in keys:
        raw = profile.get(key)
        if isinstance(raw, list):
            values.extend(str(item).strip() for item in raw if str(item).strip())
        elif raw is not None:
            text = _usable_profile_value(raw)
            if text:
                # /remember stores multi-select facts as a scalar.
                values.extend(part.strip() for part in re.split(r"[,;\n|]+", text) if part.strip())
    return values


def _profile_year(value) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", str(value or ""))
    return match.group(0) if match else ""


def _profile_value_for_field(profile: dict, el: dict) -> str:
    """Map a common application field to a grounded profile value."""
    label = " ".join(str(el.get(k) or "") for k in
                     ("question", "label", "aria_label", "name", "autocomplete")).lower()
    name = _usable_profile_value(profile.get("name"))
    parts = name.split()
    first = _usable_profile_value(profile.get("first_name")) or (parts[0] if parts else "")
    last = _usable_profile_value(profile.get("last_name")) or (" ".join(parts[1:]) if len(parts) > 1 else "")
    links = profile.get("links") if isinstance(profile.get("links"), dict) else {}
    address = _usable_profile_value(profile.get("address") or profile.get("location"))
    city, state_name = "", ""
    if address:
        address_parts = [x.strip() for x in address.split(",") if x.strip()]
        city = address_parts[0] if address_parts else address
        state_name = address_parts[1] if len(address_parts) > 1 else ""

    # High-school answers must never be inferred from a university name/year.
    if "high school" in label:
        if "graduat" in label or "year" in label:
            return (_usable_profile_value(profile.get("high_school_graduation"))
                    or _usable_profile_value(profile.get("high_school_graduation_year")))
        return _usable_profile_value(profile.get("high_school"))

    if re.search(r"\b(first|given)[ _-]*name\b|given-name", label):
        return first
    if re.search(r"\b(last|family|sur)[ _-]*name\b|family-name", label):
        return last
    if (re.search(r"\b(full[ _-]*)?name\b", label)
            and not re.search(r"company|school|employer|user", label)):
        return name
    mappings = (
        (r"e-?mail", profile.get("email")),
        (r"phone|mobile|telephone|\btel\b", profile.get("phone")),
        (r"linkedin", profile.get("linkedin") or links.get("linkedin")),
        (r"github", profile.get("github") or links.get("github")),
        (r"portfolio|personal website|website url", profile.get("portfolio") or links.get("portfolio")),
        (r"\b(city|locality)\b", profile.get("city") or city),
        (r"\b(state|province|region)\b", profile.get("state") or state_name),
        (r"zip|postal", profile.get("zip") or profile.get("postal_code")),
        (r"\bcountry\b", profile.get("country")),
        (r"street|address", address),
        (r"school|college|university", profile.get("school") or profile.get("university")),
        (r"\bdegree\b", profile.get("degree")),
        (r"graduat", profile.get("graduation_year") or profile.get("graduation")
         or _profile_year(profile.get("education"))),
        (r"current company|employer|organization", profile.get("company")),
        (r"job title|current title", profile.get("title")),
        (r"cover letter", profile.get("cover_letter")),
    )
    for pattern, value in mappings:
        if re.search(pattern, label):
            usable = _usable_profile_value(value)
            if usable:
                return usable
    try:
        import profile_store
        return _usable_profile_value(profile_store.application_answer(
            el.get("question") or el.get("label") or ""))
    except Exception:
        return ""


async def linkedin_search_jobs(args: dict) -> str:
    """Search the user's signed-in LinkedIn UI and return actual visible job results."""
    try:
        keywords = str(args.get("keywords") or "").strip()
        location = str(args.get("location") or "").strip()
        if not keywords:
            return "Error: keywords are required."
        max_results = max(1, min(50, int(args.get("max_results", 20))))
        params = {"keywords": keywords}
        if location:
            params["location"] = location
        if bool(args.get("easy_apply_only")):
            params["f_AL"] = "true"
        if bool(args.get("remote_only")):
            params["f_WT"] = "2"
        search_url = "https://www.linkedin.com/jobs/search/?" + urlencode(params)

        await _ctx()
        page = next((p for p in (_context.pages if _context else [])
                     if "linkedin.com/jobs" in p.url.lower()), None)
        if page is None:
            page = await _context.new_page()
            _owned_pages.add(page)
        await _select_page(page)
        await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await _settle(page)
        if await _challenge_on(page):
            return CHALLENGE_MSG
        _remember_source(page.url)

        results = await page.evaluate(
            """(limit) => {
              const clean = s => String(s || '').replace(/\s+/g, ' ').trim();
              const anchors = Array.from(document.querySelectorAll('a[href*="/jobs/view/"]'));
              const seen = new Set(), out = [];
              for (const a of anchors) {
                const match = a.href.match(/\/jobs\/view\/(\d+)/);
                if (!match || seen.has(match[1])) continue;
                seen.add(match[1]);
                const card = a.closest('li, [data-job-id], .job-card-container, .jobs-search-results__list-item') || a.parentElement;
                const lines = clean(card ? card.innerText : a.innerText).split(/\\n| · /).map(clean).filter(Boolean);
                const title = clean(a.innerText || a.getAttribute('aria-label')) || lines[0] || '';
                const companyNode = card && card.querySelector('.job-card-container__primary-description, .artdeco-entity-lockup__subtitle, [class*="company-name"]');
                const locationNode = card && card.querySelector('.job-card-container__metadata-item, .artdeco-entity-lockup__caption, [class*="job-location"]');
                out.push({job_id: match[1], title: title.slice(0, 180),
                  company: clean(companyNode && companyNode.innerText).slice(0, 180),
                  location: clean(locationNode && locationNode.innerText).slice(0, 180),
                  url: 'https://www.linkedin.com/jobs/view/' + match[1] + '/',
                  card_text: lines.slice(0, 8).join(' | ').slice(0, 700)});
                if (out.length >= limit) break;
              }
              return out;
            }""", max_results)
        if not results:
            body = (await page.locator("body").inner_text(timeout=10000))[:1800]
            _audit("linkedin_search", {"keywords": keywords, "location": location,
                                        "results": 0, "url": page.url}, ok=False)
            return ("No LinkedIn job cards were available in the signed-in page. "
                    "LinkedIn may require sign-in, show a verification step, or have no matches. "
                    f"Visible page text: {body}")

        for job in results:
            _remember_source(job["url"])
        artifact = ""
        if _task is not None:
            config.ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
            path = config.ARTIFACT_DIR / f"task_{_task.id}_linkedin_jobs.csv"
            with path.open("w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.DictWriter(fh, fieldnames=list(results[0].keys()))
                writer.writeheader()
                writer.writerows(results)
            _remember_artifact(path)
            artifact = str(path)
        _audit("linkedin_search", {"keywords": keywords, "location": location,
                                    "results": len(results), "url": page.url}, ok=True)
        return json.dumps({"search_url": page.url, "count": len(results),
                           "jobs": results, "artifact": artifact}, ensure_ascii=False)
    except Exception as e:
        return f"Error: LinkedIn job search failed: {e}"


async def open_job_application(args: dict) -> str:
    """Open, but never submit, the application attached to a real job page."""
    try:
        url = str(args.get("url") or "").strip()
        if url:
            opened = await browser_goto({"url": url})
            if opened.startswith("Error") or opened.startswith("STOP"):
                return opened
        page = await _ctx()
        await _settle(page)
        if await _challenge_on(page):
            return CHALLENGE_MSG
        open_dialog = page.locator('dialog[open], [role="dialog"]')
        if await open_dialog.count() and await open_dialog.first.is_visible():
            dialog_text = (await open_dialog.first.inner_text())[:500]
            if re.search(r"\bapply\s+to\b|\bapplication\b", dialog_text, re.I):
                _audit("job_application_already_open", {"url": page.url}, ok=True)
                return "Application is already open; nothing was submitted.\n" + await _fresh_digest()
        await _extract()
        candidates = [(field_id, el) for field_id, el in _last.items()
                      if gate.is_application_entry_click(el)]
        if not candidates and "linkedin.com/jobs" in page.url.lower():
            # LinkedIn relabels Easy Apply as Continue after it saves an
            # in-progress draft. This remains an entry action, not submission.
            candidates = [(field_id, el) for field_id, el in _last.items()
                          if not el.get("in_form") and
                          any(re.fullmatch(r"\s*continue\s*", str(el.get(k) or ""), re.I)
                              for k in ("text", "label", "aria_label"))]
        if not candidates:
            return ("No reversible Apply/Easy Apply entry control is visible on this job page. "
                    "It may already be in the application, the posting may be closed, or the "
                    "control may require the user to sign in. Current page: " + page.url)
        easy = [item for item in candidates if re.search(r"easy\s+apply", " ".join(
            str(item[1].get(k) or "") for k in ("text", "label", "aria_label")), re.I)]
        selected = (easy or candidates)[0]
        before_pages = set(_context.pages if _context else [])
        result = await click_element({"field_id": selected[0]})
        # LinkedIn sometimes opens its modal during the first click attempt, then
        # leaves the covered trigger waiting until Playwright times out. Verify the
        # resulting application dialog instead of believing that false negative.
        dialog_opened = False
        open_dialog = page.locator('dialog[open], [role="dialog"]')
        if await open_dialog.count() and await open_dialog.first.is_visible():
            dialog_text = (await open_dialog.first.inner_text())[:500]
            dialog_opened = bool(re.search(r"\bapply\s+to\b|\bapplication\b", dialog_text, re.I))
        if result.startswith(("Error", "User DENIED")) and not dialog_opened:
            return result
        await page.wait_for_timeout(800)
        new_pages = [p for p in (_context.pages if _context else []) if p not in before_pages]
        if new_pages:
            next_page = new_pages[-1]
            _owned_pages.add(next_page)
            await _select_page(next_page)
            await _settle(next_page)
        _remember_source(_page.url)
        _audit("job_application_opened", {"url": _page.url,
                                           "control": selected[1].get("label")}, ok=True)
        return "Application opened; nothing was submitted.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not open the job application: {e}"


async def autofill_application(args: dict) -> str:
    """Fill common application fields from saved facts and attach the saved resume."""
    try:
        import tools_fs
        profile = tools_fs.load_profile_data()
        if not profile:
            return "Error: profile.yaml has no usable profile data."
        page = await _ctx()
        await _settle(page)
        if await _challenge_on(page):
            return CHALLENGE_MSG
        elements, _, _ = await _extract()
        filled, uploaded, selected_choices, missing, errors = [], [], [], [], []
        # Scope to a modal only when that modal actually contains application
        # fields. Cookie/privacy dialogs also use role=dialog and previously made
        # Jerry ignore the real Lever form behind them.
        dialog_open = any(
            el.get("scope") == "dialog"
            and el.get("tag") in ("input", "select", "textarea")
            and (el.get("type") or "").lower() not in ("hidden", "button", "submit")
            for el in elements
        )

        resume_candidates = []
        if _task is not None:
            resume_candidates.extend(reversed(_task.attachments))
        resume_candidates.extend([
            _usable_profile_value(profile.get("resume_path")),
            _usable_profile_value(profile.get("last_upload")),
        ])
        resume_path = next((Path(p) for p in resume_candidates
                            if p and Path(p).is_file() and Path(p).suffix.lower() in
                            {".pdf", ".doc", ".docx"}), None)

        for el in elements:
            if dialog_open and el.get("scope") != "dialog":
                continue
            tag = (el.get("tag") or "").lower()
            typ = (el.get("type") or "").lower()
            if tag not in ("input", "select", "textarea") or typ in {
                    "hidden", "submit", "button", "reset", "password", "radio", "checkbox"}:
                continue
            label = (el.get("question") or el.get("label") or el.get("name") or el["id"]).strip()
            frame = _frame_for(el)
            loc = _control_locator(frame, el, el["id"])
            if typ != "file" and not await loc.is_visible():
                continue
            if typ == "file":
                if re.search(r"resume|curriculum|\bcv\b", label, re.I) and resume_path:
                    try:
                        await loc.set_input_files(str(resume_path), timeout=15000)
                        uploaded.append({"field": label, "file": resume_path.name})
                        _audit("upload", {"field": el["id"], "label": label,
                                          "file": resume_path.name}, ok=True)
                    except Exception as exc:
                        errors.append({"field": label, "error": str(exc)})
                elif el.get("required"):
                    missing.append({"field_id": el["id"], "label": label, "type": "file"})
                continue
            if el.get("value"):
                continue
            value = _profile_value_for_field(profile, el)
            if not value:
                if el.get("required"):
                    missing.append({"field_id": el["id"], "label": label, "type": typ or tag,
                                    "options": el.get("options") or ""})
                continue
            try:
                if tag == "select":
                    options = await loc.locator("option").evaluate_all(
                        """options => options.map((o, index) => ({
                          index, label: String(o.label || o.textContent || '').trim(),
                          value: String(o.value || '')
                        }))""")
                    wanted = _semantic_norm(value)
                    exact = [item for item in options
                             if _semantic_norm(item["label"]) == wanted
                             or _semantic_norm(item["value"]) == wanted]
                    if len(exact) != 1:
                        if el.get("required"):
                            missing.append({"field_id": el["id"], "label": label,
                                            "type": "select",
                                            "options": [x["label"] for x in options[:60]]})
                        continue
                    await loc.select_option(index=exact[0]["index"], timeout=8000)
                else:
                    await loc.fill(value, timeout=10000)
                filled.append({"field": label, "value": value})
                _audit("autofill", {"field": el["id"], "label": label, "value": value}, ok=True)
            except Exception as exc:
                errors.append({"field": label, "value": value, "error": str(exc)})

        await page.wait_for_timeout(400)
        # Autocomplete widgets and React fields can rerender the rest of the
        # form while text is filled. Refresh Jerry's ids before choice controls.
        elements, _, _ = await _extract()
        # Select radio/checkbox answers only when a saved answer maps to this exact
        # question. Repeated Yes/No labels are never matched across groups.
        import profile_store
        choice_groups: dict[tuple, list[dict]] = {}
        for el in elements:
            if dialog_open and el.get("scope") != "dialog":
                continue
            typ = (el.get("type") or "").lower()
            if typ not in ("radio", "checkbox"):
                continue
            question = (el.get("question") or el.get("label") or "Choice required").strip()
            key = (el.get("frame_index", 0), el.get("name") or question)
            choice_groups.setdefault(key, []).append(el)

        for group in choice_groups.values():
            question = (group[0].get("question") or group[0].get("label")
                        or "Choice required").strip()
            typ = (group[0].get("type") or "").lower()
            visible = False
            for candidate in group:
                clickable, native = await _clickable_control(
                    _frame_for(candidate), candidate, candidate["id"])
                if await clickable.is_visible() or await native.is_visible():
                    visible = True
                    break
            if not visible:
                continue

            answers = _profile_choice_values_for_question(profile, question)
            remembered = profile_store.application_answer(question)
            if remembered:
                answers.append(remembered)
            matches = []
            for candidate in group:
                option = str(candidate.get("label") or candidate.get("value") or "").strip()
                if option and any(_answer_matches_option(answer, option) for answer in answers):
                    matches.append(candidate)
            if typ == "radio" and len(matches) != 1:
                matches = []

            for candidate in matches:
                option = str(candidate.get("label") or candidate.get("value") or "").strip()
                try:
                    clickable, native = await _clickable_control(
                        _frame_for(candidate), candidate, candidate["id"])
                    if not await native.is_checked():
                        await _set_choice_state(
                            _frame_for(candidate), candidate, candidate["id"], True)
                    await page.wait_for_timeout(150)
                    if not await native.is_checked():
                        raise RuntimeError("control did not report checked after selection")
                    selected_choices.append({"field": question, "option": option, "type": typ})
                    _audit("autofill_choice", {"field": candidate["id"],
                                                "question": question,
                                                "option": option}, ok=True)
                except Exception as exc:
                    errors.append({"field": question, "option": option, "error": str(exc)})

            checked_now = []
            for candidate in group:
                native = _control_locator(_frame_for(candidate), candidate, candidate["id"])
                if await native.is_checked():
                    checked_now.append(candidate)
            if checked_now:
                continue
            options = [x.get("label") or x.get("value") for x in group]
            missing.append({
                "field_id": group[0]["id"],
                "label": question,
                "type": typ,
                "required": any(bool(x.get("required")) for x in group),
                "options": [x for x in options if x][:60],
            })
        actions = []
        for el in elements:
            if dialog_open and el.get("scope") != "dialog":
                continue
            if el.get("tag") not in ("button", "a"):
                continue
            label = (el.get("text") or el.get("label") or el.get("aria_label") or "").strip()
            if not re.search(r"\b(next|continue|review|submit application|send application)\b", label, re.I):
                continue
            loc = _control_locator(_frame_for(el), el, el["id"])
            if await loc.is_visible():
                actions.append({"field_id": el["id"], "label": label,
                                "requires_approval": gate.is_irreversible_click(el)})
        _audit("application_autofill", {"filled": len(filled), "uploaded": len(uploaded),
                                        "selected": len(selected_choices),
                                        "missing": len(missing), "errors": len(errors)},
               ok=not errors)
        return json.dumps({"page": page.url, "submitted": False, "filled": filled,
                           "uploaded": uploaded, "selected": selected_choices,
                           "missing": missing[:60],
                           "errors": errors[:20], "actions": actions[:20]}, ensure_ascii=False)
    except Exception as e:
        return f"Error: application autofill failed: {e}"


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
                "description": "Choose and verify an exact option in a native <select> dropdown. Identify it by field_id or by its visible question text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string", "description": "Element id from the digest, e.g. e5"},
                        "question": {"type": "string", "description": "Visible dropdown question/label; use when field_id is unavailable"},
                        "option": {"type": "string", "description": "Option label (or value) to select"},
                    },
                    "required": ["option"],
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
    "linkedin_search_jobs": _simple_tool(
        "linkedin_search_jobs",
        "Search real job listings through the user's signed-in LinkedIn Edge session and return sourced results plus a CSV. Use this instead of generic web search for LinkedIn jobs.",
        {"keywords": {"type": "string"},
         "location": {"type": "string"},
         "remote_only": {"type": "boolean"},
         "easy_apply_only": {"type": "boolean"},
         "max_results": {"type": "integer", "description": "1 to 50"}},
        ["keywords"], linkedin_search_jobs),
    "open_job_application": _simple_tool(
        "open_job_application",
        "Open the Apply or Easy Apply flow for a real job page without submitting it. This reversible entry action does not require final-submit approval.",
        {"url": {"type": "string", "description": "Optional LinkedIn or employer job URL"}},
        [], open_job_application),
    "autofill_application": _simple_tool(
        "autofill_application",
        "Fill common fields from Jerry's saved profile and attach the saved/uploaded resume. Never submits; returns every missing choice or fact that must be answered.",
        {}, [], autofill_application),
    "browser_back": _simple_tool("browser_back", "Go back in the current tab and read it.", fn=browser_back),
    "browser_forward": _simple_tool("browser_forward", "Go forward in the current tab and read it.", fn=browser_forward),
    "reload_page": _simple_tool(
        "reload_page",
        "Emergency recovery only: reload after repeated browser-action failures prove the page is stuck. Never use after CAPTCHA/manual verification or during a partially filled form; re-read the current DOM instead.",
        fn=reload_page),
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
    "scrape_instagram_posts": _simple_tool(
        "scrape_instagram_posts",
        "Read visible Instagram feed/profile posts and reels with exact /p/ or /reel/ links, usernames, timestamps, and visible captions. Use this before generic scraping or vision; it never likes, follows, messages, or posts.",
        {"max_posts": {"type": "integer", "description": "1 to 30, default 10"},
         "scrolls": {"type": "integer", "description": "Optional 0 to 4 controlled feed scrolls"}},
        [], scrape_instagram_posts),
    "visual_inspect": _simple_tool(
        "visual_inspect", "Use the local vision model to locate a visual-only control. Never use on CAPTCHA or verification challenges.",
        {"target": {"type": "string", "description": "Precise description of the control"}},
        ["target"], visual_inspect),
    "visual_click": _simple_tool(
        "visual_click", "Click the validated target from visual_inspect. Opaque targets require Telegram approval.",
        {}, [], visual_click),
    "find_elements": _simple_tool(
        "find_elements", "Search every extracted control by question/label/text/name, including controls omitted from a truncated digest.",
        {"query": {"type": "string"},
         "group": {"type": "string", "description": "Optional exact radio/input group name"}},
        [], find_elements),
    "choose_option": _simple_tool(
        "choose_option", "Select or clear an exact radio/checkbox choice scoped to its visible question, then verify its state. Always provide question for repeated labels such as Yes/No.",
        {"label": {"type": "string"},
         "question": {"type": "string", "description": "Visible question containing the choice"},
         "group": {"type": "string", "description": "Optional exact input group name"},
         "selected": {"type": "boolean", "description": "True to check/select (default); false to clear a checkbox"}},
        ["label"], choose_option),
})
