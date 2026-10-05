import subprocess

from cloud_coder_vm import tmux_panes
from cloud_coder_vm.tmux_panes import Pane, no_server, parse_list_panes, tmux_command


def test_parse_list_panes():
    out = "cc-repo-1\t%0\t1234\tbash\ncc-repo-1\t%3\t1300\t2.1.300\nbroken line\n"
    assert parse_list_panes(out) == [
        Pane("cc-repo-1", "%0", 1234, "bash"),
        Pane("cc-repo-1", "%3", 1300, "2.1.300"),
    ]


def test_no_server_messages():
    assert no_server("no server running on /tmp/tmux-1000/default")
    assert no_server("error connecting to /tmp/tmux-1000/default (No such file or directory)")
    assert not no_server("error connecting to /tmp/tmux-1000/default (Permission denied)")


def test_tmux_command_uses_cloud_coders_own_server(monkeypatch):
    monkeypatch.setattr(tmux_panes.os, "geteuid", lambda: 1000)
    assert tmux_command("coder") == ["tmux", "-L", "cloud-coder"]
    monkeypatch.setattr(tmux_panes.os, "geteuid", lambda: 0)
    assert tmux_command("coder", "default") == [
        "runuser",
        "-u",
        "coder",
        "--",
        "tmux",
        "-L",
        "default",
    ]


def _fake_servers(monkeypatch, outputs):
    def fake_run(args, **kw):
        rc, out, err = outputs[args[args.index("-L") + 1]]
        return subprocess.CompletedProcess(args, rc, out, err)

    monkeypatch.setattr(tmux_panes.subprocess, "run", fake_run)


def test_list_all_panes_reads_both_servers(monkeypatch):
    _fake_servers(
        monkeypatch,
        {
            "cloud-coder": (0, "cc-a-1\t%1\t10\tbash\n", ""),
            "default": (0, "test\t%1\t20\tsleep\n", ""),
        },
    )
    assert tmux_panes.list_all_panes() == [
        Pane("cc-a-1", "%1", 10, "bash", "cloud-coder"),
        Pane("test", "%1", 20, "sleep", "default"),
    ]


def test_list_all_panes_without_servers_and_on_failure(monkeypatch):
    gone = (1, "", "no server running on /tmp/tmux-1000/x")
    _fake_servers(monkeypatch, {"cloud-coder": gone, "default": gone})
    assert tmux_panes.list_all_panes() == []
    _fake_servers(monkeypatch, {"cloud-coder": gone, "default": (1, "", "Permission denied")})
    assert tmux_panes.list_all_panes() is None
