from pathlib import Path

import pytest

from cloud_coder_vm.launch import LaunchError, Layout, claude_command, resolve_target
from cloud_coder_vm.session_registry import LogicalSession

WS = Path("/home/coder/git")
LAYOUT = Layout(WS, WS / "wt", Path("/home/coder/workspace"))


def entry(name, repo, t, url=None):
    return LogicalSession(name, repo, url, f"{WS}/{repo}", f"id-{name}", 0.0, t)


def resolve(sessions, **kw):
    args = {"repo_url": None, "repo": None, "session_name": None, "new": False, "now": 100.0}
    args.update(kw)
    return resolve_target(sessions, LAYOUT, **args)


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
    assert t.session.workdir == "/home/coder/git/wt/app-2"
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


def git(*args, cwd):
    import subprocess

    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def make_clone(path, origin):
    path.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=path)
    git(
        "-c",
        "user.email=a@b",
        "-c",
        "user.name=a",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "i",
        cwd=path,
    )
    git("remote", "add", "origin", origin, cwd=path)


def tmp_layout(tmp_path):
    return Layout(tmp_path / "git", tmp_path / "git" / "wt", tmp_path / "workspace")


def test_trust_roots_have_no_duplicates(tmp_path):
    layout = tmp_layout(tmp_path)
    assert layout.trust_roots() == [tmp_path / "git", tmp_path / "git/wt", tmp_path / "workspace"]
    same = Layout(tmp_path / "workspace", tmp_path / "wt", tmp_path / "workspace")
    assert same.trust_roots() == [tmp_path / "workspace", tmp_path / "wt"]


def test_legacy_clone_is_reused_and_new_worktrees_come_from_it(tmp_path):
    from cloud_coder_vm.launch import ensure_repo, main_checkout

    layout = tmp_layout(tmp_path)
    legacy = tmp_path / "workspace" / "app"
    make_clone(legacy, "git@github.com:me/app.git")
    assert main_checkout(layout, "app") == legacy
    s1 = LogicalSession("cc-app-1", "app", "u", str(legacy), "id", 0.0, 0.0)
    assert ensure_repo(s1, layout) == "existing"
    wt = tmp_path / "git" / "wt" / "app-2"
    s2 = LogicalSession("cc-app-2", "app", "git@github.com:me/app.git", str(wt), "id2", 0.0, 0.0)
    assert ensure_repo(s2, layout, created=True) == "worktree-added"
    assert not (tmp_path / "git" / "app").exists()  # no second clone


def test_new_session_does_not_adopt_a_directory_it_did_not_create(tmp_path):
    from cloud_coder_vm.launch import ensure_repo

    layout = tmp_layout(tmp_path)
    taken = tmp_path / "git" / "wt" / "app-2"
    taken.mkdir(parents=True)
    s2 = LogicalSession("cc-app-2", "app", None, str(taken), "id2", 0.0, 0.0)
    with pytest.raises(LaunchError, match="not created by cloud-coder"):
        ensure_repo(s2, layout, created=True)


def test_existing_clone_is_adopted_only_for_the_same_repository(tmp_path):
    from cloud_coder_vm.launch import ensure_repo

    layout = tmp_layout(tmp_path)
    clone = tmp_path / "git" / "app"
    make_clone(clone, "git@github.com:me/app.git")

    def session(url):
        return LogicalSession("cc-app-1", "app", url, str(clone), "id", 0.0, 0.0)

    assert ensure_repo(session("https://github.com/me/app"), layout, created=True) == "existing"
    assert ensure_repo(session(None), layout, created=True) == "existing"  # named by the user
    with pytest.raises(LaunchError, match="not a clone of"):
        ensure_repo(session("git@github.com:other/app.git"), layout, created=True)


def test_legacy_clone_stays_the_main_checkout_when_a_current_one_appears(tmp_path):
    from cloud_coder_vm.launch import main_checkout

    layout = tmp_layout(tmp_path)
    make_clone(tmp_path / "workspace" / "app", "git@github.com:me/app.git")
    make_clone(tmp_path / "git" / "app", "git@github.com:other/app.git")  # cloned by hand
    assert main_checkout(layout, "app") == tmp_path / "workspace" / "app"
    assert main_checkout(layout, "lib") == tmp_path / "git" / "lib"


def test_new_worktree_is_refused_from_a_clone_of_another_repository(tmp_path):
    from cloud_coder_vm.launch import ensure_repo

    layout = tmp_layout(tmp_path)
    make_clone(tmp_path / "git" / "app", "git@github.com:other/app.git")
    wt = tmp_path / "git" / "wt" / "app-2"
    s2 = LogicalSession("cc-app-2", "app", "git@github.com:me/app.git", str(wt), "id", 0.0, 0.0)
    with pytest.raises(LaunchError, match="not a clone of"):
        ensure_repo(s2, layout, created=True)
    assert not wt.exists()


def test_trust_only_for_clones_verified_against_the_session_url(tmp_path):
    import json

    from cloud_coder_vm.launch import trust_session

    home = tmp_path
    layout = tmp_layout(tmp_path)
    clone = tmp_path / "git" / "app"
    make_clone(clone, "git@github.com:me/app.git")
    claude_json = home / ".claude.json"

    def trusted():
        if not claude_json.exists():
            return set()
        return set(json.loads(claude_json.read_text()).get("projects", {}))

    by_name = LogicalSession("cc-app-1", "app", None, str(clone), "id", 0.0, 0.0)
    assert trust_session(by_name, layout, home).startswith("skipped")  # every connect, not once
    assert trust_session(by_name, layout, home).startswith("skipped")
    other = LogicalSession("cc-app-1", "app", "git@github.com:x/app.git", str(clone), "i", 0, 0)
    assert trust_session(other, layout, home).startswith("skipped")
    assert trusted() == set()
    same = LogicalSession("cc-app-1", "app", "https://github.com/me/app", str(clone), "i", 0, 0)
    assert trust_session(same, layout, home) is True
    assert trusted() == {str(clone.resolve())}


def test_existing_session_does_not_add_a_worktree_from_a_foreign_clone(tmp_path):
    from cloud_coder_vm.launch import ensure_repo

    layout = tmp_layout(tmp_path)
    make_clone(tmp_path / "workspace" / "app", "git@github.com:other/app.git")  # appeared later
    wt = tmp_path / "git" / "wt" / "app-2"
    s2 = LogicalSession("cc-app-2", "app", "git@github.com:me/app.git", str(wt), "id", 0.0, 0.0)
    with pytest.raises(LaunchError, match="not a clone of"):
        ensure_repo(s2, layout, created=False)
