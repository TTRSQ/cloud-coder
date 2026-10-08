import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from cloud_coder_vm import launch, paths, session_close, session_registry
from cloud_coder_vm.regenerable_caches import NOT_A_CACHE
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
        "deleted_caches": [],
        "branch": "deleted",
    }
    assert not (home / "git" / "wt" / "app-2").exists()
    assert branches(home) == {"main"}
    assert registered(home) == {"cc-app-1"}


@pytest.mark.parametrize("change", ["uncommitted", "untracked", "ignored", "unpushed"])
def test_unsaved_or_ignored_work_keeps_everything(home, change):
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


@pytest.mark.parametrize("failing", ["cloud-coder", "default"])
def test_a_tmux_failure_keeps_everything(home, monkeypatch, failing):
    """Only a session tmux does not know is "absent"; any other failure may leave Claude
    Code running, so its worktree must stay."""
    fail = ["sh", "-c", "echo 'permission denied' >&2; exit 1", "tmux"]
    no_server = ["sh", "-c", "echo 'no server running on /tmp/x' >&2; exit 1", "tmux"]
    monkeypatch.setattr(
        session_close,
        "tmux_command",
        lambda user, socket="cloud-coder": fail if socket == failing else no_server,
    )
    with pytest.raises(CloseError, match="permission denied"):
        close(CONFIG, home, "cc-app-2")
    assert (home / "git" / "wt" / "app-2").exists()
    assert registered(home) == {"cc-app-1", "cc-app-2"}


def test_a_session_left_on_the_default_tmux_server_is_not_closed(home, monkeypatch):
    """`kill_tmux_session` only reaches cloud-coder's server: closing a session an older
    cloud-coder started on the default one would remove the worktree under a running
    Claude Code."""

    def tmux_command(user, socket="cloud-coder"):
        return ["true"] if socket == "default" else ["false"]

    monkeypatch.setattr(session_close, "tmux_command", tmux_command)
    with pytest.raises(CloseError, match="default tmux server.*nothing was closed"):
        close(CONFIG, home, "cc-app-2")
    assert (home / "git" / "wt" / "app-2").exists()
    assert registered(home) == {"cc-app-1", "cc-app-2"}


def test_a_new_task_does_not_reuse_the_name_of_a_closed_pushed_session(home):
    """cc-app-2's branch stays on the remote (often with a pull request): a new session
    named cc-app-2 would start on, and push to, that branch."""
    worktree = home / "git" / "wt" / "app-2"
    git("commit", "-q", "--allow-empty", "-m", "work", cwd=worktree)
    git("push", "-q", "origin", "cloud-coder/cc-app-2", cwd=worktree)
    close(CONFIG, home, "cc-app-2")
    sessions = session_registry.load(paths.registry_path(home))
    target = launch.resolve_target(
        sessions,
        launch.Layout.of(CONFIG, home),
        repo_url=None,
        repo="app",
        session_name=None,
        new=False,
        has_prompt=True,
        now=0,
    )
    assert target.session.name == "cc-app-3"


TAG = b"Signature: 8a477f597d28d172789f06886806bc55\n"
WORKTREE = ("git", "wt", "app-2")


def ignore(home, *patterns):
    """Ignore ``patterns`` in every worktree of the clone, as a .gitignore would."""
    exclude = home / "git" / "app" / ".git" / "info" / "exclude"
    exclude.write_text("".join(f"{p}\n" for p in patterns))


def make_caches(worktree):
    """What `uv sync`, `pytest` and `cargo build` (in a crate in a subdirectory) leave."""
    (worktree / ".venv" / "bin").mkdir(parents=True)
    (worktree / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (worktree / "pkg" / "__pycache__").mkdir(parents=True)
    (worktree / "pkg" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\0" * 5000)
    target = worktree / "crates" / "a" / "target"
    (target / "debug" / ".fingerprint").mkdir(parents=True)
    (target / "debug" / "deps").mkdir()
    (target / "debug" / "deps" / "a-0123").write_bytes(b"\0" * 100_000)
    (target / "CACHEDIR.TAG").write_bytes(TAG)


@pytest.fixture
def caches(home):
    ignore(home, ".venv/", "__pycache__/", "target", ".env", "out/", "link")
    make_caches(home.joinpath(*WORKTREE))
    return home


def test_a_worktree_with_only_caches_left_is_closed(caches):
    result = close(CONFIG, caches, "cc-app-2")
    assert result["worktree"] == "removed"
    assert sorted(result["deleted_caches"]) == [".venv/", "crates/a/target/", "pkg/__pycache__/"]
    assert result["branch"] == "deleted"
    assert not caches.joinpath(*WORKTREE).exists()
    assert registered(caches) == {"cc-app-1"}


@pytest.mark.parametrize(
    ("make", "blocker"),
    [
        (lambda wt: (wt / ".env").write_text("TOKEN=x"), "ignored file: .env"),
        (lambda wt: (wt / "out").mkdir() or (wt / "out" / "raw.parquet").write_text(""), "out/"),
        (
            lambda wt: (wt / "crates" / "a" / "target" / "results.csv").write_text("1"),
            "crates/a/target/ (has results.csv",
        ),
        (lambda wt: (wt / ".venv" / "notes.db").write_text(""), ".venv/ (has notes.db"),
        (  # a cache inside protected data does not make the data a cache
            lambda wt: shutil.copytree(wt / "crates" / "a" / "target", wt / "out" / "target"),
            "ignored file: out/",
        ),
    ],
)
def test_anything_but_caches_keeps_everything(caches, make, blocker):
    worktree = caches.joinpath(*WORKTREE)
    make(worktree)
    with pytest.raises(CloseError, match="nothing was closed") as refused:
        close(CONFIG, caches, "cc-app-2")
    assert blocker in str(refused.value)
    assert "--discard-ignored" not in str(refused.value)  # deleting them is for a person
    assert (worktree / ".venv" / "pyvenv.cfg").exists()  # not even the caches go
    assert (worktree / "crates" / "a" / "target" / "CACHEDIR.TAG").exists()
    assert registered(caches) == {"cc-app-1", "cc-app-2"}


def test_a_symlink_to_a_target_elsewhere_is_kept_and_refuses(caches):
    worktree = caches.joinpath(*WORKTREE)
    shared = caches / "shared-target"
    shutil.move(worktree / "crates" / "a" / "target", shared)
    (worktree / "crates" / "a" / "target").symlink_to(shared)
    with pytest.raises(CloseError, match=r"crates/a/target \(a symlink\)"):
        close(CONFIG, caches, "cc-app-2")
    assert (shared / "CACHEDIR.TAG").exists()


def test_discard_ignored_deletes_other_ignored_files_but_not_what_links_point_to(caches):
    worktree = caches.joinpath(*WORKTREE)
    (worktree / ".env").write_text("TOKEN=x")
    outside = caches / "outside"
    outside.mkdir()
    (outside / "keep").write_text("data")
    (worktree / "link").symlink_to(outside)
    result = close(CONFIG, caches, "cc-app-2", discard_ignored=True)
    assert result["worktree"] == "removed"
    assert sorted(result["discarded_ignored"]) == [".env", "link"]
    assert (outside / "keep").read_text() == "data"


@pytest.mark.parametrize("change", ["uncommitted", "untracked", "unpushed"])
def test_discard_ignored_never_discards_uncommitted_or_unpushed_work(caches, change):
    worktree = caches.joinpath(*WORKTREE)
    (worktree / ".env").write_text("TOKEN=x")
    (worktree / "f.txt").write_text("work")
    if change != "untracked":
        git("add", "f.txt", cwd=worktree)
    if change == "unpushed":
        git("commit", "-q", "-m", "work", cwd=worktree)
    with pytest.raises(CloseError, match="nothing was closed"):
        close(CONFIG, caches, "cc-app-2", discard_ignored=True)
    assert (worktree / ".env").exists() and (worktree / "f.txt").exists()
    assert registered(caches) == {"cc-app-1", "cc-app-2"}


def test_dry_run_reports_blockers_caches_and_size_and_changes_nothing(caches):
    worktree = caches.joinpath(*WORKTREE)
    (worktree / ".env").write_text("TOKEN=x")
    before = sorted(caches.joinpath("git").rglob("*"))
    preview = close(CONFIG, caches, "cc-app-2", dry_run=True)
    assert sorted(caches.joinpath("git").rglob("*")) == before
    assert registered(caches) == {"cc-app-1", "cc-app-2"}
    assert preview["dry_run"] is True and preview["closable"] is False
    assert preview["worktree"] == str(worktree)
    assert preview["tmux"] == "absent"
    assert preview["blockers"] == [f"ignored file: .env ({NOT_A_CACHE})"]
    sizes = {c["path"]: c["bytes"] for c in preview["caches"]}
    assert set(sizes) == {".venv/", "crates/a/target/", "pkg/__pycache__/"}
    assert sizes["crates/a/target/"] >= 100_000
    assert preview["caches_bytes"] == sum(sizes.values())
    assert preview["worktree_bytes"] >= preview["caches_bytes"]
    assert preview["ended_processes"] == []
    discarding = close(CONFIG, caches, "cc-app-2", dry_run=True, discard_ignored=True)
    assert discarding["closable"] is True and discarding["discarded_ignored"] == [".env"]


def test_dry_run_of_the_main_checkout(home):
    preview = close(CONFIG, home, "cc-app-1", dry_run=True)
    assert preview["worktree"] == "kept (main checkout)"
    assert preview["closable"] is True and preview["worktree_bytes"] == 0


@pytest.fixture
def sleeper(home):
    proc = subprocess.Popen(["sleep", "60"], cwd=home.joinpath(*WORKTREE))
    yield proc
    proc.kill()
    proc.wait()


def test_a_process_using_the_worktree_keeps_everything(home, sleeper):
    with pytest.raises(CloseError, match=f"process {sleeper.pid} \\(sleep\\) uses the worktree"):
        close(CONFIG, home, "cc-app-2")
    assert home.joinpath(*WORKTREE).exists()
    assert registered(home) == {"cc-app-1", "cc-app-2"}


def test_a_process_left_after_the_tmux_session_ends_keeps_the_worktree(home, sleeper, monkeypatch):
    """A process of the session's own panes that survives ending them (nohup, say)."""
    monkeypatch.setattr(session_close, "session_pids", lambda name: {sleeper.pid})
    monkeypatch.setattr(session_close, "EXIT_WAIT_SECONDS", 0.2)
    with pytest.raises(CloseError, match="processes still use .* the worktree is kept"):
        close(CONFIG, home, "cc-app-2")
    assert home.joinpath(*WORKTREE).exists()
    assert registered(home) == {"cc-app-1", "cc-app-2"}


def test_a_mount_point_in_the_worktree_keeps_everything(home, monkeypatch, tmp_path):
    worktree = home.joinpath(*WORKTREE)
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 8:1 / / rw - ext4 /dev/sda1 rw\n"
        f"90 22 0:50 / {worktree}/data\\040set rw - tmpfs tmpfs rw\n"
        f"91 22 0:51 / {worktree}-other rw - tmpfs tmpfs rw\n"
    )
    monkeypatch.setattr(session_close, "MOUNTINFO", mountinfo)
    assert session_close.mount_points_under(worktree, mountinfo) == [f"{worktree}/data set"]
    monkeypatch.setattr(session_close, "mount_points_under", lambda d: [f"{worktree}/data set"])
    with pytest.raises(CloseError, match="mount point: .*data set"):
        close(CONFIG, home, "cc-app-2", discard_ignored=True)
    assert worktree.exists()


def test_parse_status_keeps_renames_and_ignored_apart():
    output = "R  new.py\0old.py\0?? a b.txt\0!! target/\0!! x.pyc\0 M f\0"
    assert session_close.parse_status(output) == (
        ["new.py", "a b.txt", "f"],
        ["target/", "x.pyc"],
    )


def test_cli_dry_run_prints_the_preview(caches, monkeypatch, capsys):
    from cloud_coder_vm import cli as vm_cli

    monkeypatch.setattr(vm_cli, "_load_config", lambda: CONFIG)
    monkeypatch.setattr(vm_cli.Path, "home", classmethod(lambda cls: caches))
    assert vm_cli.main(["close-session", "--session", "cc-app-2", "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["closable"] is True
    (caches.joinpath(*WORKTREE) / ".env").write_text("x")
    assert vm_cli.main(["close-session", "--session", "cc-app-2"]) == 1
    assert "ignored file: .env" in json.loads(capsys.readouterr().out)["error"]
    assert vm_cli.main(["close-session", "--session", "cc-app-2", "--discard-ignored"]) == 0
    assert json.loads(capsys.readouterr().out)["discarded_ignored"] == [".env"]


def test_caches_that_ignore_themselves_are_judged_whole(home):
    """pytest, ruff, mypy and uv write a .gitignore of "*" into their caches: git then
    lists what is inside, not the directory, when the repository does not ignore it."""
    worktree = home.joinpath(*WORKTREE)
    (worktree / ".pytest_cache" / "v" / "cache").mkdir(parents=True)
    (worktree / ".pytest_cache" / "CACHEDIR.TAG").write_bytes(TAG)
    (worktree / ".pytest_cache" / ".gitignore").write_text("*\n")
    (worktree / ".venv" / "bin").mkdir(parents=True)
    (worktree / ".venv" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    (worktree / ".venv" / ".gitignore").write_text("*\n")
    preview = close(CONFIG, home, "cc-app-2", dry_run=True)
    assert preview["blockers"] == []
    assert sorted(c["path"] for c in preview["caches"]) == [".pytest_cache/", ".venv/"]
    (worktree / ".venv" / "notes.db").write_text("")  # ignored by .venv/.gitignore too
    with pytest.raises(CloseError, match=r"\.venv/ \(has notes\.db"):
        close(CONFIG, home, "cc-app-2")


def test_a_git_repository_in_an_ignored_path_is_never_discarded(caches):
    worktree = caches.joinpath(*WORKTREE)
    (worktree / "out" / "experiment").mkdir(parents=True)
    git("init", "-q", str(worktree / "out" / "experiment"), cwd=worktree)
    with pytest.raises(CloseError, match="git repository in an ignored path: .*out/experiment"):
        close(CONFIG, caches, "cc-app-2", discard_ignored=True)
    assert (worktree / "out" / "experiment" / ".git").exists()


def test_untracked_files_count_whatever_status_showuntrackedfiles_says(home):
    worktree = home.joinpath(*WORKTREE)
    git("config", "status.showUntrackedFiles", "no", cwd=worktree)
    (worktree / "f.txt").write_text("work")
    with pytest.raises(CloseError, match="uncommitted or untracked: f.txt"):
        close(CONFIG, home, "cc-app-2")
    assert (worktree / "f.txt").exists()


def test_the_session_s_own_long_running_processes_are_listed_in_the_preview(home, monkeypatch):
    sleeper = subprocess.Popen(["sleep", "60"])
    try:
        monkeypatch.setattr(session_close, "session_pids", lambda name: {sleeper.pid})
        preview = close(CONFIG, home, "cc-app-2", dry_run=True)
    finally:
        sleeper.kill()
        sleeper.wait()
    assert preview["ended_processes"] == [f"{sleeper.pid} (sleep)"]
    assert preview["tmux"] == "running, ended by close"


def test_a_process_that_only_maps_a_file_of_the_worktree_keeps_everything(caches, tmp_path):
    """Python run from outside as .venv/bin/python: its exe is the interpreter the
    symlink points to, and the .venv's libraries are mapped, not open."""
    library = caches.joinpath(*WORKTREE) / ".venv" / "lib" / "ext.so"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"\0" * 4096)
    script = (
        "import mmap, sys, time\n"
        "f = open(sys.argv[1], 'rb'); m = mmap.mmap(f.fileno(), 0, prot=mmap.PROT_READ)\n"
        "f.close(); print('mapped', flush=True); time.sleep(60)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", script, str(library)], cwd=tmp_path, stdout=subprocess.PIPE
    )
    try:
        assert proc.stdout.readline() == b"mapped\n"
        with pytest.raises(CloseError, match=f"process {proc.pid} .* uses the worktree"):
            close(CONFIG, caches, "cc-app-2")
    finally:
        proc.kill()
        proc.wait()
    assert library.exists()


@pytest.mark.parametrize("discard_ignored", [False, True])
def test_a_cache_named_directory_with_tracked_files_is_not_judged_whole(home, discard_ignored):
    """A repository that commits node_modules (a JavaScript GitHub Action, say): only
    the ignored paths in it are judged, and nothing tracked is deleted."""
    worktree = home.joinpath(*WORKTREE)
    (worktree / "package.json").write_text("{}")
    (worktree / "node_modules" / "dep").mkdir(parents=True)
    (worktree / "node_modules" / "dep" / "index.js").write_text("module.exports = 1\n")
    git("add", "package.json", "node_modules", cwd=worktree)
    git("commit", "-q", "-m", "vendor", cwd=worktree)
    git("push", "-q", "origin", "cloud-coder/cc-app-2", cwd=worktree)
    ignore(home, ".env")
    (worktree / "node_modules" / "dep" / ".env").write_text("TOKEN=x")
    preview = close(CONFIG, home, "cc-app-2", dry_run=True, discard_ignored=discard_ignored)
    assert preview["caches"] == []
    if not discard_ignored:
        assert preview["blockers"] == [f"ignored file: node_modules/dep/.env ({NOT_A_CACHE})"]
        return
    assert preview["discarded_ignored"] == ["node_modules/dep/.env"]
    close(CONFIG, home, "cc-app-2", discard_ignored=True)
    assert not worktree.exists()  # removed by git: index.js is on the remote


def test_inspecting_a_worktree_leaves_its_index_alone(home, monkeypatch):
    """The session may be running `git add` or `git commit` in the worktree: inspecting
    it must not take the index lock to refresh the index."""
    monkeypatch.delenv("GIT_OPTIONAL_LOCKS", raising=False)  # git would take the lock
    worktree = home.joinpath(*WORKTREE)
    (worktree / "f.txt").write_text("committed")
    git("add", "f.txt", cwd=worktree)
    git("commit", "-q", "-m", "f", cwd=worktree)
    os.utime(worktree / "f.txt", (0, 0))  # stat differs from the index: git would refresh it
    index = Path(
        git("rev-parse", "--path-format=absolute", "--git-path", "index", cwd=worktree)
        .stdout.decode()
        .strip()
    )
    before = index.stat()
    found = session_close.inspect(worktree, False, set())
    assert not [b for b in found.blockers if b.startswith("uncommitted")]
    after = index.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
