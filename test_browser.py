"""Track C integration test — no LLM. Opens a VISIBLE Chromium window (expected).

Starts a local http.server for mock_form/, drives the DevFest registration form
through the actual tool fns, and exercises the safety gate with a stub confirm.
Prints PASS/FAIL per assertion; exit code 1 if anything failed.
"""
import asyncio
import subprocess
import sys
import time
import urllib.request

# Windows consoles often default to cp1252 — page text may contain arbitrary unicode.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import config
import gate
import tools_browser

RESULTS = []


def check(name: str, cond, detail: str = ""):
    ok = bool(cond)
    line = f"{'PASS' if ok else 'FAIL'}: {name}"
    if detail and not ok:
        line += f" — {detail}"
    print(line)
    RESULTS.append(ok)


# --- pure gate.py unit asserts (no browser) ---
def gate_tests():
    check(
        "gate: type=submit -> True",
        gate.is_irreversible_click(
            {"tag": "button", "type": "submit", "text": "Go", "aria_label": "", "in_form": True}
        ),
    )
    check(
        "gate: 'Continue' link not in form -> False",
        not gate.is_irreversible_click(
            {"tag": "a", "type": "", "text": "Continue", "aria_label": "", "in_form": False}
        ),
    )
    check(
        "gate: 'Sign up' text -> True",
        gate.is_irreversible_click(
            {"tag": "a", "type": "", "text": "Sign up", "aria_label": "", "in_form": False}
        ),
    )
    check(
        "gate: in-form button type='button' neutral text -> False",
        not gate.is_irreversible_click(
            {"tag": "button", "type": "button", "text": "Show more", "aria_label": "", "in_form": True}
        ),
    )


def find_id(label_sub: str, tags=None):
    """Find an element id from the last extraction by label substring."""
    for eid, el in tools_browser._last.items():
        if tags is not None and el.get("tag") not in tags:
            continue
        if label_sub.lower() in (el.get("label") or "").lower():
            return eid
    return None


SUMMARIES = []


async def confirm_stub(summary: str) -> bool:
    SUMMARIES.append(summary)
    return True


async def browser_tests():
    T = tools_browser.TOOLS
    tools_browser.configure(confirm=confirm_stub)

    url = f"http://localhost:{config.MOCK_FORM_PORT}/index.html"
    digest = await T["browser_goto"]["fn"]({"url": url})
    print("\n--- initial digest ---\n" + digest + "\n----------------------\n")

    check("digest contains 'Full name'", "Full name" in digest, digest[:200])
    check("digest contains 'Email'", "Email" in digest)
    check("digest contains 'T-shirt'", "T-shirt" in digest)
    check("digest flags a [SUBMIT — requires approval] line", "[SUBMIT — requires approval]" in digest)
    check(
        "iframe elements extracted (f#e# ids)",
        any(eid.startswith("f") for eid in tools_browser._last),
        f"ids: {list(tools_browser._last)}",
    )

    name_id = find_id("full name", tags=("input",))
    email_id = find_id("email", tags=("input",))
    shirt_id = find_id("t-shirt", tags=("select",))
    diet_id = find_id("dietary", tags=("textarea",))
    news_id = find_id("newsletter", tags=("input",))
    check("located ids for name/email/shirt/diet/newsletter",
          all([name_id, email_id, shirt_id, diet_id, news_id]),
          f"{name_id=} {email_id=} {shirt_id=} {diet_id=} {news_id=}")

    r = await T["fill_field"]["fn"]({"field_id": name_id, "value": "Ada Lovelace"})
    check("fill full name", not r.startswith("Error") and 'value="Ada Lovelace"' in r, r[:200])
    r = await T["fill_field"]["fn"]({"field_id": email_id, "value": "ada@example.com"})
    check("fill email", not r.startswith("Error"), r[:200])
    r = await T["select_option"]["fn"]({"field_id": shirt_id, "option": "L"})
    check("select T-shirt 'L'", not r.startswith("Error") and 'value="L"' in r, r[:200])
    r = await T["fill_field"]["fn"]({"field_id": diet_id, "value": "Vegetarian, no nuts"})
    check("fill dietary textarea", not r.startswith("Error"), r[:200])
    r = await T["fill_field"]["fn"]({"field_id": news_id, "value": "true"})
    check("check newsletter box", not r.startswith("Error") and "checked" in r, r[:200])

    reg_id = find_id("register")
    check("located Register button", reg_id is not None)
    r = await T["click_element"]["fn"]({"field_id": reg_id})
    print("\n--- click result ---\n" + r + "\n--------------------\n")

    check("confirm stub WAS called", len(SUMMARIES) == 1, f"called {len(SUMMARIES)} times")
    if SUMMARIES:
        print("--- approval summary ---\n" + SUMMARIES[0] + "\n------------------------\n")
        check("summary contains filled name", "Ada Lovelace" in SUMMARIES[0], SUMMARIES[0])
        check("summary mentions the button label", "Register now" in SUMMARIES[0])

    page_text = await T["read_page"]["fn"]({})
    check(
        "landed on success page ('registered')",
        "registered" in r.lower() or "registered" in page_text.lower(),
        page_text[:200],
    )

    shot = await T["screenshot_page"]["fn"]({})
    check("screenshot returns 'screenshot saved: ' prefix", shot.startswith("screenshot saved: "), shot)
    if shot.startswith("screenshot saved: "):
        path = shot[len("screenshot saved: "):].strip()
        try:
            with open(path, "rb") as f:
                magic = f.read(8)
            check("screenshot file is a PNG", magic[:4] == b"\x89PNG", repr(magic))
        except OSError as e:
            check("screenshot file is a PNG", False, str(e))

    check("page_or_none() returns live page before shutdown", tools_browser.page_or_none() is not None)
    await tools_browser.shutdown()
    check("page_or_none() is None after shutdown", tools_browser.page_or_none() is None)


def wait_for_server(url: str, tries: int = 40) -> bool:
    for _ in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def main():
    gate_tests()

    server = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(config.MOCK_FORM_PORT),
         "--directory", str(config.MOCK_FORM_DIR)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        up = wait_for_server(f"http://localhost:{config.MOCK_FORM_PORT}/index.html")
        check("mock form http server is up", up)
        if up:
            asyncio.run(browser_tests())
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()

    passed = sum(RESULTS)
    print(f"\n{passed}/{len(RESULTS)} checks passed")
    sys.exit(0 if all(RESULTS) else 1)


if __name__ == "__main__":
    main()
