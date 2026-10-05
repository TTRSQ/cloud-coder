import contextlib
import subprocess

import pytest

from cloud_coder_vm import paths, session_close, session_registry
from cloud_coder_vm.session_close import CloseError, close
from cloud_coder_vm.session_registry import LogicalSession
from cloud_coder_vm.system_files import VmConfig

CONFIG = VmConfig("coder", 0, "git", "git/wt", False, 0, [], False, False, 0, False, None, None, "")
IDENTITY = ["-c", "user.email=a@b", "-c", "user.name=a"]


def git(*args, cwd):
    return subprocess.run(["git", *IDENTITY, *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A HOME with a clone of a local remote, a worktree session cc-app-2 on a branch
    that is only at the remote's commit, and the main checkout's session cc-app-1."""
    # a tmux socket directory of its own with no server: nothing to kill, and never the
    # server of the tmux this test may run in
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", str(tmp_path))
    monkeypatch.setattr(session_close, "state_lock", contextlib.nullcontext)
    remote = tmp_path / "remote.git"
    git("init", "-q", "--bare", "-b", "main", str(remote), cwd=tmp_path)
    main = tmp_path / "git" / "app"
    main.parent.mkdir()
    git("clone", "-q", str(remote), str(main), cwd=tmp_path)
    git("commit", "-q", "--allow-empty", "-m", "init", cwd=main)
    git("push", "-q", "origin", "main", cwd=main)
    worktree = tmp_path / "git" / "wt" / "app-2"
    git("worktree", "add", "-q", "-b", "cloud-coder/cc-app-2", str(worktree), cwd=main)
    sessions = {
        name: LogicalSession(name, "app", str(remote), str(workdir), f"id-{name}", 0.0, 0.0)
        for name, workdir in [("cc-app-1", main), ("cc-app-2", worktree)]
    }
    registry = paths.registry_path(tmp_path)
    registry.parent.mkdir(parents=True)
    session_registry.save(registry, sessions)
    return tmp_path


def registered(home):
    return set(session_registry.load(paths.registry_path(home)))


def branches(home):
    out = subprocess.run(
        ["git", "branch", "--format=%(refname:short)"],
        cwd=home / "git" / "app",
        capture_output=True,
        text=True,
    )
    return set(out.stdout.split())


def test_a_clean_worktree_session_is_removed_with_its_branch(home):
    assert close(CONFIG, home, "cc-app-2") == {
        "session": "cc-app-2",
        "tmux": "absent",
        "worktree": "removed",
        "branch": "deleted",
    }
    assert not (home / "git" / "wt" / "app-2").exists()
    assert branches(home) == {"main"}
    assert registered(home) == {"cc-app-1"}


@pytest.mark.parametrize("change", ["uncommitted", "untracked", "ignored", "unpushed"])
def test_unsaved_work_keeps_everything(home, change):
    worktree = home / "git" / "wt" / "app-2"
    (worktree / "f.txt").write_text("work")
    if change == "ignored":  # e.g. a .env: git worktree remove would delete it
        (home / "git" / "app" / ".git" / "info" / "exclude").write_text("f.txt\n")
    if change in ("uncommitted", "unpushed"):
        git("add", "f.txt", cwd=worktree)
    if change == "unpushed":
        git("commit", "-q", "-m", "work", cwd=worktree)
    with pytest.raises(CloseError, match="nothing was closed"):
        close(CONFIG, home, "cc-app-2")
    assert (worktree / "f.txt").exists()
    assert "cloud-coder/cc-app-2" in branches(home)
    assert registered(home) == {"cc-app-1", "cc-app-2"}


def test_the_main_checkout_is_kept(home):
    (home / "git" / "app" / "f.txt").write_text("work")
    assert close(CONFIG, home, "cc-app-1")["worktree"] == "kept (main checkout)"
    assert (home / "git" / "app" / "f.txt").exists()
    assert registered(home) == {"cc-app-2"}


def test_unknown_session(home):
    with pytest.raises(CloseError, match="unknown session"):
        close(CONFIG, home, "cc-x-9")


def test_a_tmux_failure_keeps_everything(home, monkeypatch):
    """Only a session tmux does not know is "absent"; any other failure may leave Claude
    Code running, so its worktree must stay."""
    fail = ["sh", "-c", "echo 'permission denied' >&2; exit 1", "tmux"]
    monkeypatch.setattr(session_close, "tmux_command", lambda user: fail)
    with pytest.raises(CloseError, match="permission denied"):
        close(CONFIG, home, "cc-app-2")
    assert (home / "git" / "wt" / "app-2").exists()
    assert registered(home) == {"cc-app-1", "cc-app-2"}
