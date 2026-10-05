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


def test_unknown_session_and_bad_lines(home):
    with pytest.raises(session_screen.ScreenError, match="unknown session"):
        session_screen.read(home, "cc-x-1", 10)
    with pytest.raises(session_screen.ScreenError, match="at least 1"):
        session_screen.read(home, "cc-a-1", 0)


def test_reads_the_claude_pane_and_its_state(home, monkeypatch):
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
    out = session_screen.read(home, "cc-a-1", 2)
    assert calls[0][:3] == ["tmux", "-L", "cloud-coder"]
    assert calls[1] == [
        "tmux",
        "-L",
        "cloud-coder",
        "capture-pane",
        "-p",
        "-J",
        "-S",
        "-2",
        "-t",
        "%3",
    ]
    assert out == {
        "session": "cc-a-1",
        "tmux": "running",
        "pane": "%3",
        "claude": "running",
        "claude_state": "READY",
        "output": "> fix it\n● Done.",
    }


def test_session_without_tmux(home, monkeypatch):
    monkeypatch.setattr(
        session_screen.subprocess,
        "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "can't find session"),
    )
    assert session_screen.read(home, "cc-a-1", 5)["tmux"] == "absent"
