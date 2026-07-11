"""Playwright browser/form tools for Pocket Agent (Track C).

Async Playwright only. Module-level lazy singleton browser context reusing
one visible Chromium page. All tool fns return strings and never raise.
Irreversible clicks (per gate.is_irreversible_click) go through an async
confirm callback injected via configure().
"""
import time
import urllib.request

from playwright.async_api import async_playwright, TimeoutError as PWTimeoutError

import config
import gate

# --- module state ---
_pw = None
_context = None
_page = None
_last: dict[str, dict] = {}   # data-agent-id -> element info from last extraction
_preauth = False
_cdp = False   # True when attached to the user's real Edge over CDP (co-drive)


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


def configure(confirm=None, preauth=False):
    """Inject the approval callback (async (summary)->bool) and preauth flag."""
    global _confirm_cb, _preauth
    _confirm_cb = confirm if confirm is not None else _default_confirm
    _preauth = bool(preauth)


async def _ctx():
    """Lazy singleton page. Prefer co-drive (attach to the user's real Edge over CDP);
    otherwise fall back to our own persistent Chromium (also the demo backup)."""
    global _pw, _context, _page, _cdp
    if _page is not None and not _page.is_closed():
        return _page
    if _context is None:
        _pw = await async_playwright().start()
        if _cdp_available():
            # Co-drive: adopt the user's real Edge session. Never spoof, never bypass.
            browser = await _pw.chromium.connect_over_cdp(config.CDP_URL)
            _context = browser.contexts[0] if browser.contexts else await browser.new_context()
            _cdp = True
        else:
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
    # Prefer the most recently active real tab in co-drive mode.
    if _context.pages:
        _page = _context.pages[-1]
    else:
        _page = await _context.new_page()
    return _page


async def shutdown():
    """Disconnect (CDP: never close the user's Edge) or close our own browser."""
    global _pw, _context, _page, _cdp
    try:
        if _cdp and _context is not None:
            # connect_over_cdp: close the browser *connection*, leaving Edge running.
            await _context.browser.close()
        elif _context is not None:
            await _context.close()
    except Exception:
        pass
    try:
        if _pw is not None:
            await _pw.stop()
    except Exception:
        pass
    _pw = _context = _page = None
    _cdp = False
    _last.clear()


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
    if (!isVisible(el)) continue;
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

  let n = 0;
  const out = [];
  for (const el of kept) {
    n += 1;
    const id = prefix + n;
    el.setAttribute('data-agent-id', id);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || (tag === 'input' ? 'text' : '')).toLowerCase();
    let options = '';
    if (tag === 'select') {
      options = Array.from(el.options).slice(0, 12).map((o) => o.label || o.value).join('|');
    }
    out.push({
      id: id,
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
            return await _fresh_digest()
        page = await _ctx()
        await page.goto(url, wait_until="load", timeout=20000)
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
        return text[: config.PAGE_TEXT_MAX]
    except Exception as e:
        return f"Error: could not read page: {e}"


async def extract_form_fields(args: dict) -> str:
    try:
        return await _fresh_digest()
    except Exception as e:
        return f"Error: extraction failed: {e}"


async def fill_field(args: dict) -> str:
    field_id = str(args.get("field_id") or "").strip()
    value = args.get("value", "")
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        frame = _frame_for(el)
        loc = frame.locator(f'[data-agent-id="{field_id}"]')
        if (el.get("type") or "").lower() in ("checkbox", "radio"):
            desired = str(value).strip().lower() not in _FALSEY
            await loc.set_checked(desired, timeout=15000)
        else:
            await loc.fill(str(value), timeout=15000)
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
        return f"Attached {p.name} to {field_id}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not upload to {field_id}: {e}"


async def click_element(args: dict) -> str:
    field_id = str(args.get("field_id") or "").strip()
    el = _last.get(field_id)
    if el is None:
        return f"Error: field {field_id} not in the last extraction — call extract_form_fields."
    try:
        page = await _ctx()
        if gate.is_irreversible_click(el) and not _preauth:
            el_for_gate = dict(el)
            try:
                el_for_gate["page"] = await page.title() or page.url
            except Exception:
                el_for_gate["page"] = page.url
            summary = gate.summarize_submission(el_for_gate, _form_values())
            approved = await _confirm_cb(summary)
            if not approved:
                return DENIED_MSG
        frame = _frame_for(el)
        loc = frame.locator(f'[data-agent-id="{field_id}"]')
        await loc.click(timeout=15000)
        try:
            await page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeoutError:
            pass
        return f"Clicked {field_id}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not click {field_id}: {e}"


async def screenshot_page(args: dict) -> str:
    try:
        page = await _ctx()
        config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
        path = config.SCREENSHOT_DIR / f"page_{int(time.time())}.png"
        await page.screenshot(path=str(path))
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
        return f"Switched to tab {idx}.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not switch tab: {e}"


async def new_tab(args: dict) -> str:
    try:
        await _ctx()
        url = str(args.get("url") or "").strip()
        page = await _context.new_page()
        await _select_page(page)
        if url:
            try:
                await page.goto(url, wait_until="load", timeout=20000)
            except Exception as e:
                return f"Opened a new tab but could not load {url}: {e}"
        return f"Opened new tab.\n" + await _fresh_digest()
    except Exception as e:
        return f"Error: could not open new tab: {e}"


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
