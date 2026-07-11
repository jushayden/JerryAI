"""Read/change/verify smoke test for complex hidden-option controls on Tesla; never orders."""
import asyncio
import json

import config
import state
import tools_browser


URL = "https://www.tesla.com/modely/design?#overview"


async def deny(summary):
    raise AssertionError(f"Tesla option test must never reach a consequential action: {summary}")


def radio(label_fragment: str, *, exclude_name: str = ""):
    needle = label_fragment.lower()
    return next((
        (field_id, el) for field_id, el in tools_browser._last.items()
        if el.get("type") == "radio"
        and needle in (el.get("label") or "").lower()
        and (not exclude_name or el.get("name") != exclude_name)
    ), (None, None))


async def main():
    task = state.TaskRecord(
        id="tesla-live", text="select red exterior and white interior",
        origin="badge", source_url=URL)
    tools_browser.configure(confirm=deny, task=task)
    try:
        digest = await tools_browser.browser_goto({"url": URL})
        assert not digest.startswith(("Error", "STOP:")), digest

        result = await tools_browser.choose_option({"label": "Ultra Red", "group": "PAINT"})
        assert not result.startswith("Error"), result
        red_id, red = radio("Ultra Red")
        assert red and red.get("checked"), "Ultra Red was not verified selected"

        interiors = "[]"
        for _ in range(8):
            interiors = await tools_browser.find_elements({"query": "Interior"})
            if "All Black Interior" in interiors or "Zen Grey Interior" in interiors:
                break
            await tools_browser.scroll_page({"amount": 1200})
            await tools_browser.extract_form_fields({})
        assert "Interior" in interiors, "Interior options were not discoverable"
        unavailable = await tools_browser.choose_option(
            {"label": "White Interior", "group": "PREMIUM_PACKAGE"})
        assert unavailable.startswith("Error: no exact choice"), unavailable

        page = tools_browser.page_or_none()
        selected = await page.evaluate("""() => Array.from(document.querySelectorAll(
          'input[type=radio]:checked')).map(e => ({name:e.name, label:e.getAttribute('aria-label'), value:e.value}))""")
        assert any(x["name"] == "PAINT" and "Red" in (x["label"] or "") for x in selected)
        print("TESLA LIVE PASS — red selected; white unavailable; choices reported",
              interiors, json.dumps(selected, ensure_ascii=False))
    finally:
        await tools_browser.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
