import json
import subprocess

import pytest

from cloud_coder_vm import paths, session_screen
from cloud_coder_vm.session_registry import LogicalSession, save
from cloud_coder_vm.session_state import SessionState
from cloud_coder_vm.tmux_panes import Pane


def test_visible_text_drops_trailing_blank_rows_and_spaces():
    assert session_screen.visible_text("> hi   \n● Done.\n\n\n\n") == "> hi\n● Done."


@pytest.fixture
def home(tmp_path):
    session = LogicalSession("cc-a-1", "a", None, "/w/a", "id", 0.0, 0.0)
    save(paths.registry_path(tmp_path), {"cc-a-1": session})
    return tmp_path


def test_reads_the_claude_pane_and_its_state(monkeypatch):
    calls = []

    def fake_run(args, **kw):
        calls.append(args)
        if "list-panes" in args:
            return subprocess.CompletedProcess(args, 0, "cc-a-1\t%3\t100\tbash\n", "")
        return subprocess.CompletedProcess(args, 0, "old\n> fix it\n● Done.\n\n\n", "")

    monkeypatch.setattr(session_screen.subprocess, "run", fake_run)
    monkeypatch.setattr(
        session_screen, "claude_pane", lambda panes: (Pane("cc-a-1", "%3", 100, "bash"), 200)
    )
    monkeypatch.setattr(
        session_screen.session_state,
        "load_all",
        lambda d: [SessionState("pane-3", "s", "READY", tmux_pane="%3", claude_pid=200)],
    )
    out = session_screen.read("cc-a-1")
    assert calls[0][:3] == ["tmux", "-L", "cloud-coder"]
    assert calls[1] == [
        "tmux",
        "-L",
        "cloud-coder",
        "capture-pane",
        "-p",
        "-J",
        "-t",
        "%3",
    ]
    assert out == {
        "session": "cc-a-1",
        "tmux": "running",
        "pane": "%3",
        "claude": "running",
        "claude_state": "READY",
        "output": "old\n> fix it\n● Done.",
    }


def test_session_without_tmux(monkeypatch):
    monkeypatch.setattr(
        session_screen.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "can't find session"),
    )
    assert session_screen.read("cc-a-1")["tmux"] == "absent"


def test_cli_adds_the_latest_responses_from_the_transcript(home, monkeypatch, capsys):
    from cloud_coder_vm import cli as vm_cli

    monkeypatch.setattr(vm_cli.Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(
        session_screen.subprocess,
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
    assert out["tmux"] == "absent"
    assert out["messages"] == [{"text": "Done.", "timestamp": "2026-10-15T02:40:50.000Z"}]

    for count in ("0", "11"):
        assert vm_cli.main(["read-session", "--session", "cc-a-1", "--messages", count]) == 1
        assert json.loads(capsys.readouterr().out) == {"error": "messages must be 1 to 10"}
