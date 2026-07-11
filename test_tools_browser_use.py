"""Offline tests for tools_browser_use.py — no browser, no model, no network.
The real browser-use package may be installed; these tests exercise OUR gate/plumbing
with fake nodes/sessions, and only touch browser_use for ActionResult/Tools shape.
Run: python test_tools_browser_use.py"""
import asyncio
import tempfile
from pathlib import Path

import config

config.BROWSER_USE_ENABLED = True  # before importing the module under test

import gate
import tools_browser_use as tbu


# --- fakes ---

class FakeNode:
    def __init__(self, tag="button", attrs=None, text="", parent=None, ax_name=""):
        self._tag = tag
        self.attributes = attrs or {}
        self._text = text
        self.parent_node = parent
        self.ax_node = type("AX", (), {"name": ax_name})() if ax_name else None

    @property
    def tag_name(self):
        return self._tag

    def get_all_children_text(self, max_depth=-1):
        return self._text

    def get_meaningful_text_for_llm(self):
        return self._text


class FakeSession:
    def __init__(self, node):
        self.node = node

    async def get_element_by_index(self, index):
        return self.node

    async def get_current_page_url(self):
        return "https://example.com/apply"

    async def get_current_page_title(self):
        return "Careers"


dispatched: list = []  # (kind, payload) recorded by the patched dispatch seams


async def _fake_dispatch_click(browser_session, node):
    dispatched.append(("click", node))
    return None


async def _fake_dispatch_keys(browser_session, keys):
    dispatched.append(("keys", keys))
    return None


tbu._dispatch_click = _fake_dispatch_click
tbu._dispatch_keys = _fake_dispatch_keys


class FakeCDP:
    """cdp_client whose Runtime.evaluate returns a fixed in-form verdict."""
    def __init__(self, in_form=True, boom=False):
        outer = self

        class _Runtime:
            async def evaluate(self, params=None):
                if boom:
                    raise RuntimeError("cdp exploded")
                return {"result": {"value": outer._in_form}}

        class _Send:
            Runtime = _Runtime()

        self._in_form = in_form
        self.send = _Send()


async def allow(summary):
    allow.calls.append(summary)
    return True


async def deny(summary):
    deny.calls.append(summary)
    return False


async def boom_confirm(summary):
    raise AssertionError(f"gate fired when it should not have: {summary}")


def _reset(confirm):
    allow.calls, deny.calls = [], []
    dispatched.clear()
    tbu.configure(confirm=confirm, task=None)


async def main():
    # --- registry contract shape ---
    assert "web_agent" in tbu.TOOLS
    entry = tbu.TOOLS["web_agent"]
    assert set(entry) == {"schema", "fn"}
    fn = entry["schema"]["function"]
    assert fn["name"] == "web_agent" and "task" in fn["parameters"]["properties"]
    assert asyncio.iscoroutinefunction(entry["fn"])
    print("PASS TOOLS registry contract shape")

    # --- _element_dict / _node_in_form drive the gate exactly like tools_browser ---
    form = FakeNode(tag="form")
    submit_input = FakeNode(tag="input", attrs={"type": "submit"}, parent=form)
    btn_in_form = FakeNode(tag="button", text="Continue", parent=form)
    buy_div = FakeNode(tag="div", text="Buy now")
    plain_link = FakeNode(tag="a", text="Read the docs")
    typed_button = FakeNode(tag="button", attrs={"type": "button"}, text="Show more", parent=form)
    assert gate.is_irreversible_click(tbu._element_dict(submit_input))
    assert gate.is_irreversible_click(tbu._element_dict(btn_in_form))
    assert gate.is_irreversible_click(tbu._element_dict(buy_div))
    assert not gate.is_irreversible_click(tbu._element_dict(plain_link))
    assert not gate.is_irreversible_click(tbu._element_dict(typed_button))
    assert tbu._node_in_form(btn_in_form) and not tbu._node_in_form(buy_div)
    print("PASS _element_dict/_node_in_form gate decisions")

    # --- gated click: deny -> DENIED, nothing dispatched ---
    _reset(deny)
    r = await tbu._gated_click_impl(3, FakeSession(submit_input))
    assert tbu.DENIED_MSG in (r.extracted_content or ""), r.extracted_content
    assert getattr(r, "error", None) in (None, "")  # denial must NOT burn max_failures
    assert dispatched == [], "denied click must not dispatch ClickElementEvent"
    assert len(deny.calls) == 1 and "High-impact action" in deny.calls[0]
    assert "Careers — https://example.com/apply" in deny.calls[0]
    print("PASS gated click deny (no dispatch, content not error)")

    # --- gated click: approve -> dispatched exactly once ---
    _reset(allow)
    r = await tbu._gated_click_impl(4, FakeSession(btn_in_form))
    assert "Clicked" in (r.extracted_content or "")
    assert [k for k, _ in dispatched] == ["click"]
    assert len(allow.calls) == 1
    print("PASS gated click approve (dispatched once)")

    # --- non-submit element: gate never fires ---
    _reset(boom_confirm)
    r = await tbu._gated_click_impl(5, FakeSession(plain_link))
    assert "Clicked" in (r.extracted_content or "")
    assert [k for k, _ in dispatched] == ["click"]
    print("PASS ungated click bypasses confirm")

    # --- missing element ---
    class GoneSession(FakeSession):
        async def get_element_by_index(self, index):
            return None
    r = await tbu._gated_click_impl(9, GoneSession(None))
    assert "not available" in (r.extracted_content or "")
    print("PASS click on missing element -> guidance, no crash")

    # --- send_keys: Escape passes ungated ---
    _reset(boom_confirm)
    r = await tbu._gated_send_keys_impl("Escape", FakeSession(plain_link), FakeCDP(in_form=True))
    assert "Sent keys" in (r.extracted_content or "")
    assert dispatched == [("keys", "Escape")]
    print("PASS send_keys Escape ungated")

    # --- send_keys: Enter in a form -> gated; deny dispatches nothing ---
    _reset(deny)
    r = await tbu._gated_send_keys_impl("Enter", FakeSession(plain_link), FakeCDP(in_form=True))
    assert tbu.DENIED_MSG in (r.extracted_content or "")
    assert dispatched == []
    print("PASS send_keys Enter gated (deny -> no dispatch)")

    # --- send_keys: Enter with a broken CDP check -> over-gates by design ---
    _reset(deny)
    r = await tbu._gated_send_keys_impl("Enter", FakeSession(plain_link), FakeCDP(boom=True))
    assert tbu.DENIED_MSG in (r.extracted_content or "")
    assert dispatched == []
    print("PASS send_keys Enter over-gates when the form check fails")

    # --- send_keys: Enter NOT in a form -> ungated ---
    _reset(boom_confirm)
    r = await tbu._gated_send_keys_impl("Enter", FakeSession(plain_link), FakeCDP(in_form=False))
    assert "Sent keys" in (r.extracted_content or "")
    assert dispatched == [("keys", "Enter")]
    print("PASS send_keys Enter outside forms is ungated")

    # --- excluded/replaced action lists guard the no-bypass invariant ---
    assert "evaluate" in tbu.EXCLUDED_ACTIONS, "evaluate could form.submit() around the gate"
    for name in ("write_file", "replace_file", "read_file", "save_as_pdf"):
        assert name in tbu.EXCLUDED_ACTIONS
    assert set(tbu.REPLACED_ACTIONS) == {"click", "send_keys"}
    print("PASS exclusion/replacement lists")

    # --- real Tools registry (uses installed browser-use): replaced + gone ---
    try:
        tools = tbu._build_tools()
        names = set(tools.registry.registry.actions)
        for name in tbu.EXCLUDED_ACTIONS:
            assert name not in names, f"{name} must be excluded"
        for name in ("click", "send_keys", "look_at_page", "upload_file", "done"):
            assert name in names, f"{name} missing from registry"
        desc = tools.registry.registry.actions["click"].description
        assert "approval" in desc.lower()
        print("PASS _build_tools against installed browser-use "
              f"({len(names)} actions registered)")
    except ImportError:
        print("SKIP _build_tools (browser-use not installed)")

    # --- flag off -> web_agent refuses and TOOLS stays empty on fresh import ---
    config.BROWSER_USE_ENABLED = False
    r = await tbu.web_agent({"task": "anything"})
    assert r.startswith("Error") and "disabled" in r
    import importlib
    fresh = importlib.reload(tbu)
    assert fresh.TOOLS == {}
    config.BROWSER_USE_ENABLED = True
    importlib.reload(tbu)
    print("PASS flag off -> disabled error + empty TOOLS")

    # --- upload allowlist: only resume/uploads/downloads, missing dirs tolerated ---
    real_upload, real_download = config.UPLOAD_DIR, config.DOWNLOAD_DIR
    with tempfile.TemporaryDirectory() as td:
        config.UPLOAD_DIR = Path(td) / "uploads"
        config.DOWNLOAD_DIR = Path(td) / "downloads"
        config.UPLOAD_DIR.mkdir()
        (config.UPLOAD_DIR / "resume.pdf").write_bytes(b"x")
        paths = tbu._allowed_upload_paths()
        assert any(p.endswith("resume.pdf") for p in paths)
        assert all(("uploads" in p) or ("downloads" in p) or p for p in paths)
    config.UPLOAD_DIR, config.DOWNLOAD_DIR = real_upload, real_download
    print("PASS _allowed_upload_paths")

    print("\nALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
