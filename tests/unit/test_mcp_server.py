import json

import anyio
import pytest
from mcp import Client

from cloud_coder import connect, gce, mcp_server
from cloud_coder.config import Config

CFG = Config(project="p")


def call(name: str, arguments: dict | None = None):
    async def run():
        async with Client(mcp_server.build_server(CFG)) as client:
            return await client.call_tool(name, arguments or {})

    return anyio.run(run)


def result_json(result) -> dict:
    assert not result.is_error, result.content[0].text
    return json.loads(result.content[0].text)


def test_tools_never_take_the_target_vm():
    async def run():
        async with Client(mcp_server.build_server(CFG)) as client:
            return (await client.list_tools()).tools

    tools = {t.name: t for t in anyio.run(run)}
    assert set(tools) == {
        "status",
        "up",
        "stop",
        "start_session",
        "send_prompt",
        "read_session",
    }
    for tool in tools.values():
        assert not {"project", "zone", "instance"} & set(tool.input_schema.get("properties", {}))
    assert tools["status"].annotations.read_only_hint
    assert tools["stop"].annotations.destructive_hint


def test_up_returns_without_waiting(monkeypatch):
    seen = {}

    def fake_up(cfg, *, wait):
        seen["wait"] = wait
        return connect.UpResult("started", ready=False, agent_installed=False)

    monkeypatch.setattr(connect, "up", fake_up)
    assert result_json(call("up")) == {
        "vm_action": "started",
        "ready": False,
        "agent_installed": False,
    }
    assert seen == {"wait": False}


def test_session_tools_need_a_ready_vm(monkeypatch):
    monkeypatch.setattr(connect, "up", lambda cfg, wait: connect.UpResult("started", False, False))
    monkeypatch.setattr(connect, "launch", lambda *a, **kw: pytest.fail("launched"))
    result = call("start_session", {"repo": "https://github.com/o/r.git"})
    assert result.is_error and "call `up`" in result.content[0].text


def test_send_prompt_launches_the_session_without_agent_forwarding(monkeypatch):
    monkeypatch.setattr(connect, "up", lambda cfg, wait: connect.UpResult("running", True, False))
    seen = {}

    def fake_launch(cfg, repo, **kw):
        seen.update(kw, repo=repo)
        return {"session": kw["session"], "prompt": "sent"}

    monkeypatch.setattr(connect, "launch", fake_launch)
    assert result_json(call("send_prompt", {"session": "cc-a-1", "text": "go"}))["prompt"] == "sent"
    assert seen == {"repo": None, "session": "cc-a-1", "prompt": "go", "forward_agent": False}


def test_agent_errors_reach_the_client(monkeypatch):
    monkeypatch.setattr(connect, "up", lambda cfg, wait: connect.UpResult("running", True, False))

    def busy(*a, **kw):
        raise connect.AgentError("Claude Code in this session is BUSY; prompt not sent")

    monkeypatch.setattr(connect, "launch", busy)
    result = call("send_prompt", {"session": "cc-a-1", "text": "go"})
    assert result.is_error and "is BUSY; prompt not sent" in result.content[0].text


def test_read_session_does_not_start_the_vm(monkeypatch):
    monkeypatch.setattr(gce, "describe", lambda cfg: gce.Vm(gce.STOPPED))
    monkeypatch.setattr(connect, "up", lambda *a, **kw: pytest.fail("started the VM"))
    result = call("read_session", {"session": "cc-a-1"})
    assert result.is_error and "the VM is stopped" in result.content[0].text

    monkeypatch.setattr(gce, "describe", lambda cfg: gce.Vm(gce.RUNNING))
    monkeypatch.setattr(
        connect, "read_session", lambda cfg, s, n: {"session": s, "output": f"{n} lines"}
    )
    assert result_json(call("read_session", {"session": "cc-a-1", "lines": 5}))["output"] == (
        "5 lines"
    )
    assert call("read_session", {"session": "cc-a-1", "lines": 0}).is_error


def test_stop_does_not_wait(monkeypatch):
    monkeypatch.setattr(gce, "stop", lambda cfg, wait: gce.STOPPING if not wait else gce.STOPPED)
    assert result_json(call("stop")) == {"vm": "stopping"}
