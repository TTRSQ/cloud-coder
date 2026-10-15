import json
import subprocess

import pytest

from cloud_coder_vm import paths, session_status
from cloud_coder_vm.session_registry import LogicalSession, save
from cloud_coder_vm.session_state import SessionState, dialog
from cloud_coder_vm.tmux_panes import Pane


@pytest.fixture
def home(tmp_path):
    session = LogicalSession("cc-a-1", "a", None, "/w/a", "id", 0.0, 0.0)
    save(paths.registry_path(tmp_path), {"cc-a-1": session})
    return tmp_path


def test_reads_claude_code_s_state_and_dialogs_never_the_screen(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, "cc-a-1\t%3\t100\tbash\n", "")

    asking = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "AskUserQuestion",
        "tool_input": {
            "questions": [
                {
                    "question": "Which one?",
                    "header": "Pick",
                    "multiSelect": False,
                    "options": [{"label": "A", "description": "a"}, {"label": "B"}],
                }
            ]
        },
    }
    monkeypatch.setattr(session_status.subprocess, "run", fake_run)
    monkeypatch.setattr(
        session_status, "claude_pane", lambda panes: (Pane("cc-a-1", "%3", 100, "bash"), 200)
    )
    state = SessionState("pane-3", "s", "BUSY", tmux_pane="%3", claude_pid=200)
    state.open_dialogs = [dialog(asking)]
    monkeypatch.setattr(session_status.session_state, "load_all", lambda d: [state])
    assert session_status.read("cc-a-1") == {
        "tmux": "running",
        "claude": "running",
        "claude_state": "BUSY",
        "waiting_for": [
            {
                "kind": "question",
                "questions": [
                    {"question": "Which one?", "options": ["A", "B"], "multi_select": False}
                ],
            }
        ],
    }
    assert [c[3] for c in calls] == ["list-panes"]  # no capture-pane


def test_session_without_tmux(monkeypatch):
    monkeypatch.setattr(
        session_status.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "can't find session"),
    )
    assert session_status.read("cc-a-1") == {
        "tmux": "absent",
        "claude": "not running",
        "claude_state": None,
        "waiting_for": [],
    }


def test_cli_adds_the_latest_responses_from_the_transcript(home, monkeypatch, capsys):
    from cloud_coder_vm import cli as vm_cli

    monkeypatch.setattr(vm_cli.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        session_status.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "can't find session"),
    )
    assert vm_cli.main(["read-session", "--session", "cc-x-1"]) == 1
    assert json.loads(capsys.readouterr().out) == {"error": "unknown session 'cc-x-1'"}

    assert vm_cli.main(["read-session", "--session", "cc-a-1"]) == 0
    assert json.loads(capsys.readouterr().out)["messages"] == []

    path = home / ".claude/projects/-w-a/id.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-10-15T02:40:50.000Z",
                "message": {"id": "m", "content": [{"type": "text", "text": "Done."}]},
            }
        )
        + "\n"
    )
    assert vm_cli.main(["read-session", "--session", "cc-a-1", "--messages", "3"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert set(out) == {"session", "tmux", "claude", "claude_state", "waiting_for", "messages"}
    assert out["messages"] == [{"text": "Done.", "timestamp": "2026-10-15T02:40:50.000Z"}]

    for count in ("0", "11"):
        assert vm_cli.main(["read-session", "--session", "cc-a-1", "--messages", count]) == 1
        assert json.loads(capsys.readouterr().out) == {"error": "messages must be 1 to 10"}
