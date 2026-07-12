"""Discoverable regression tests for Jerry's runtime reliability fixes."""
import asyncio
from pathlib import Path

import agent
import config
import main
import state
import tools_browser
import tools_browser_use
import tools_email
import tools_fs
import tools_system


def test_text_tool_call_parser_handles_qwen_format():
    content = """I'll do that.\n<function=browser_goto>
<parameter=url>https://example.test/apply</parameter>
</function>\n</tool_call>"""
    calls = agent._text_tool_calls(content)
    assert calls == [{"function": {"name": "browser_goto",
                                    "arguments": {"url": "https://example.test/apply"}}}]


def test_text_tool_call_parser_rejects_normal_text():
    assert agent._text_tool_calls("I finished the requested task.") == []


def test_agent_executes_textual_tool_call_instead_of_false_success(monkeypatch):
    responses = [
        {"message": {"role": "assistant", "content":
                     "<function=ping><parameter=value>ok</parameter></function></tool_call>"}},
        {"message": {"role": "assistant", "content": "completed", "tool_calls": []}},
    ]
    called = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def chat(self, **kwargs):
            return responses.pop(0)

    async def ping(args):
        called.append(args)
        return "pong"

    tool = {"ping": {"schema": {"type": "function", "function": {
        "name": "ping", "description": "test", "parameters": {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"]}}}, "fn": ping}}
    monkeypatch.setattr(agent.ollama, "AsyncClient", FakeClient)
    result = asyncio.run(agent.run_task("do it", extra_tools=tool, require_action=True,
                                        status_cb=lambda _: asyncio.sleep(0)))
    assert result == "completed"
    assert called == [{"value": "ok"}]


def test_task_tool_routing_keeps_applications_on_direct_browser():
    task = state.TaskRecord(id="test01", text="fill out this application and upload my resume",
                            origin="badge", source_url="https://example.test/apply")
    selected = main._task_tools(task)
    assert "browser_goto" in selected and "upload_file" in selected
    assert "web_agent" not in selected
    assert "set_volume" not in selected and "scan_inbox" not in selected


def test_task_tool_routing_scopes_os_and_email():
    os_task = state.TaskRecord(id="test02", text="set the volume to 20", origin="telegram")
    assert set(main._task_tools(os_task)) == set(tools_system.TOOLS)
    mail_task = state.TaskRecord(id="test03", text="scan my gmail inbox", origin="telegram")
    assert set(main._task_tools(mail_task)) == set(tools_email.TOOLS)


def test_long_research_can_keep_browser_use_when_enabled():
    task = state.TaskRecord(id="test04", text="research and compare these products across multiple sites",
                            origin="telegram")
    selected = main._task_tools(task)
    assert "browser_goto" in selected
    if config.BROWSER_USE_ENABLED:
        assert "web_agent" in selected


def test_telegram_uploads_are_readable_but_not_writable(monkeypatch, tmp_path):
    home = tmp_path / "home"
    uploads = tmp_path / "artifacts" / "uploads"
    downloads = tmp_path / "artifacts" / "downloads"
    home.mkdir()
    uploads.mkdir(parents=True)
    downloads.mkdir(parents=True)
    resume = uploads / "resume.txt"
    resume.write_text("resume body", encoding="utf-8")
    monkeypatch.setattr(config, "SANDBOX_ROOT", home)
    monkeypatch.setattr(config, "UPLOAD_DIR", uploads)
    monkeypatch.setattr(config, "DOWNLOAD_DIR", downloads)
    assert tools_fs._safe(str(resume), allow_artifacts=True) == resume.resolve()
    try:
        tools_fs._safe(str(resume))
        raise AssertionError("artifact path unexpectedly writable")
    except ValueError:
        pass
    assert asyncio.run(tools_fs.read_file({"path": str(resume)})) == "resume body"


def test_stale_element_resolves_by_semantic_fingerprint(monkeypatch):
    old = {"id": "old", "dom_id": "stable", "data_id": "", "tag": "input",
           "type": "text", "name": "email", "value": "", "label": "Email"}
    new = dict(old, id="new")
    tools_browser._last.clear()
    tools_browser._known.clear()
    tools_browser._known["old"] = old

    async def fake_extract():
        tools_browser._last["new"] = new
        return [new], "", ""

    monkeypatch.setattr(tools_browser, "_extract", fake_extract)
    assert asyncio.run(tools_browser._resolve_element("old")) == ("new", new)
