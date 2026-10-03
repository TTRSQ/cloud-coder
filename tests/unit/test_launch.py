from pathlib import Path

import pytest

from cloud_coder_vm.launch import LaunchError, claude_command, resolve_target
from cloud_coder_vm.session_registry import LogicalSession

WS = Path("/home/coder/workspace")


def entry(name, repo, t, url=None):
    return LogicalSession(name, repo, url, f"{WS}/{repo}", f"id-{name}", 0.0, t)


def resolve(sessions, **kw):
    args = {"repo_url": None, "repo": None, "session_name": None, "new": False, "now": 100.0}
    args.update(kw)
    return resolve_target(sessions, WS, **args)


def test_first_connect_creates_session_in_main_checkout():
    t = resolve({}, repo_url="git@github.com:o/app.git")
    assert t.created
    assert t.session.name == "cc-app-1"
    assert t.session.workdir == str(WS / "app")
    assert t.session.repo_url == "git@github.com:o/app.git"


def test_reconnect_reuses_latest_session_of_repo():
    s = {"cc-app-1": entry("cc-app-1", "app", 1), "cc-app-2": entry("cc-app-2", "app", 5)}
    t = resolve(s, repo_url="git@github.com:o/app.git")
    assert not t.created and t.session.name == "cc-app-2"


def test_new_creates_worktree_session_and_never_reuses():
    s = {"cc-app-1": entry("cc-app-1", "app", 1, url="u")}
    t = resolve(s, repo="app", new=True)
    assert t.created and t.session.name == "cc-app-2"
    assert t.session.workdir == str(WS / "app.worktrees" / "2")
    assert t.session.repo_url == "u"
    assert t.session.claude_session_id != s["cc-app-1"].claude_session_id


def test_no_repo_means_latest_session():
    s = {"cc-a-1": entry("cc-a-1", "a", 1), "cc-b-1": entry("cc-b-1", "b", 3)}
    assert resolve(s).session.name == "cc-b-1"
    with pytest.raises(LaunchError):
        resolve({})


def test_explicit_session():
    s = {"cc-a-1": entry("cc-a-1", "a", 1)}
    assert resolve(s, session_name="cc-a-1").session.name == "cc-a-1"
    with pytest.raises(LaunchError):
        resolve(s, session_name="cc-x-9")


def test_claude_command_new_vs_resume():
    s = entry("cc-a-1", "a", 1)
    new = claude_command(Path("/home/coder"), s, resume=False)
    assert new == (
        "CLOUD_CODER_SESSION=cc-a-1 /home/coder/.local/bin/claude "
        "--session-id id-cc-a-1 --remote-control cc-a-1"
    )
    assert "--resume id-cc-a-1" in claude_command(Path("/home/coder"), s, resume=True)


def test_claude_command_reads_first_prompt_from_file_verbatim(tmp_path):
    import subprocess

    s = entry("cc-a-1", "a", 1)
    prompt = "line 1 'single' \"double\" $HOME `id`\n-starts-with-dash\n日本語"
    prompt_file = tmp_path / "p 1.txt"
    prompt_file.write_text(prompt)
    command = claude_command(Path("/home/coder"), s, resume=False, prompt_file=prompt_file)
    assert command.endswith(f"-- \"$(cat '{prompt_file}'; rm -f '{prompt_file}')\"")
    # run the same shell expansion with printf standing in for claude
    probe = command.replace("/home/coder/.local/bin/claude", "printf '%s\\0'")
    out = subprocess.run(["bash", "-c", probe], capture_output=True, text=True, check=True)
    argv = out.stdout.split("\0")[:-1]
    assert argv[-2:] == ["--", prompt]
    assert not prompt_file.exists()


def test_regroup_command(monkeypatch):
    import grp

    from cloud_coder_vm.launch import regroup_command

    monkeypatch.setattr(grp, "getgrnam", lambda name: type("G", (), {"gr_gid": 999})())
    assert regroup_command("docker", {999}, {1000}) == [
        "sg",
        "docker",
        "-c",
        'exec "${SHELL:-/bin/bash}" -l',
    ]
    assert regroup_command("docker", {999}, {999}) == []  # server already has it
    assert regroup_command("docker", {999}, None) == []  # new server inherits it
    assert regroup_command("docker", {1000}, {1000}) == []  # this login lacks it too


def test_same_repo_name_from_another_owner_is_refused():
    s = {"cc-app-1": entry("cc-app-1", "app", 1, url="git@github.com:me/app.git")}
    assert not resolve(s, repo_url="https://github.com/me/app").created  # same repo
    with pytest.raises(LaunchError, match="cloned from"):
        resolve(s, repo_url="git@github.com:someone-else/app.git")
