import json

import pytest

from agent import Agent, ToolRegistry, ToolSpec, extract_tool_calls
from gate import Gate
from notify import AutoApproveNotifier


class FakeOllama:
    """Returns scripted responses in order; repeats the last one if exhausted."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.responses) > 1:
            return {"message": self.responses.pop(0)}
        return {"message": self.responses[0]}


def tool_call_msg(name, arguments):
    return {"role": "assistant", "content": "",
            "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def final_msg(text):
    return {"role": "assistant", "content": text}


@pytest.fixture
def echo_registry():
    registry = ToolRegistry()

    async def echo(ctx, text: str) -> str:
        return f"echo: {text}"

    async def boom(ctx) -> str:
        raise RuntimeError("kaboom")

    registry.register(ToolSpec("echo", echo, {"type": "function", "function": {
        "name": "echo", "description": "echo",
        "parameters": {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"]}}}))
    registry.register(ToolSpec("boom", boom, {"type": "function", "function": {
        "name": "boom", "description": "always fails",
        "parameters": {"type": "object", "properties": {}, "required": []}}}))
    return registry


def make_agent(tool_ctx, registry, responses):
    gate = Gate(approver=AutoApproveNotifier(), log=tool_ctx.log, timeout_s=1)
    return Agent(tool_ctx.cfg, registry, gate, tool_ctx.log,
                 client=FakeOllama(responses))


async def test_normal_tool_call_then_answer(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [
        tool_call_msg("echo", {"text": "hi"}),
        final_msg("done: hi"),
    ])
    assert await agent.run_task("say hi", tool_ctx) == "done: hi"
    # tool result reached the model
    history = agent.client.calls[-1]["messages"]
    assert any(m.get("role") == "tool" and "echo: hi" in m["content"] for m in history)


async def test_stringified_arguments(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [
        tool_call_msg("echo", '{"text": "stringy"}'),
        final_msg("ok"),
    ])
    assert await agent.run_task("t", tool_ctx) == "ok"
    history = agent.client.calls[-1]["messages"]
    assert any("echo: stringy" in m.get("content", "") for m in history if m.get("role") == "tool")


async def test_tool_json_in_content(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [
        final_msg('{"name": "echo", "arguments": {"text": "from-content"}}'),
        final_msg("parsed it"),
    ])
    assert await agent.run_task("t", tool_ctx) == "parsed it"


async def test_fenced_json_in_content(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [
        final_msg('Let me call a tool:\n```json\n{"name": "echo", "parameters": {"text": "fenced"}}\n```'),
        final_msg("ok"),
    ])
    assert await agent.run_task("t", tool_ctx) == "ok"


async def test_unknown_tool_self_corrects(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [
        tool_call_msg("hallucinated_tool", {}),
        final_msg("recovered"),
    ])
    assert await agent.run_task("t", tool_ctx) == "recovered"
    history = agent.client.calls[-1]["messages"]
    err = [m for m in history if m.get("role") == "tool"][0]["content"]
    assert "unknown tool" in err and "echo" in err


async def test_tool_exception_becomes_error_result(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [
        tool_call_msg("boom", {}),
        final_msg("survived"),
    ])
    assert await agent.run_task("t", tool_ctx) == "survived"
    history = agent.client.calls[-1]["messages"]
    err = [m for m in history if m.get("role") == "tool"][0]["content"]
    assert err.startswith("ERROR: RuntimeError: kaboom")


async def test_repeat_guard(tool_ctx, echo_registry):
    # model repeats the identical call forever; guard kicks in, then cap ends it
    agent = make_agent(tool_ctx, echo_registry, [
        tool_call_msg("echo", {"text": "loop"}),
    ])
    answer = await agent.run_task("t", tool_ctx)
    assert "step limit" in answer
    history = agent.client.calls[-1]["messages"]
    repeats = [m for m in history if m.get("role") == "tool"
               and "already called" in m.get("content", "")]
    assert repeats  # guard message was injected


async def test_iteration_cap(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [tool_call_msg("echo", {"text": "x"})])
    answer = await agent.run_task("t", tool_ctx)
    assert "step limit" in answer
    assert len(agent.client.calls) == tool_ctx.cfg.max_agent_iterations


async def test_bad_arguments_recoverable(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [
        tool_call_msg("echo", {"wrong_param": 1}),
        final_msg("fixed"),
    ])
    assert await agent.run_task("t", tool_ctx) == "fixed"
    history = agent.client.calls[-1]["messages"]
    err = [m for m in history if m.get("role") == "tool"][0]["content"]
    assert err.startswith("ERROR: bad arguments")


def test_extract_no_calls_plain_text():
    calls, malformed = extract_tool_calls({"content": "just an answer"})
    assert calls == [] and malformed is False


def test_extract_malformed_flags_nudge():
    calls, malformed = extract_tool_calls(
        {"content": '{"name": "echo", "arguments": {broken'})
    assert calls == [] and malformed is True


async def test_events_logged(tool_ctx, echo_registry):
    agent = make_agent(tool_ctx, echo_registry, [
        tool_call_msg("echo", {"text": "hi"}),
        final_msg("done"),
    ])
    await agent.run_task("t", tool_ctx)
    lines = (tool_ctx.cfg.state_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    kinds = [json.loads(l)["kind"] for l in lines]
    for expected in ("task_received", "agent_iteration", "tool_call",
                     "gate_decision", "tool_result", "task_done"):
        assert expected in kinds
