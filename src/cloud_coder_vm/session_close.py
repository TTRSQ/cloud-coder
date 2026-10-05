"""Close a cloud-coder session: end its tmux session, remove its worktree and forget it.

Work that exists only on the VM is never deleted: a worktree with uncommitted changes
or commits not on a remote is kept, and the session is not closed. The main checkout is
never removed, and Claude Code's transcript stays on the disk.
"""

import subprocess
from pathlib import Path

from cloud_coder_vm import paths, session_registry
from cloud_coder_vm.launch import Layout, main_checkout
from cloud_coder_vm.state_lock import state_lock
from cloud_coder_vm.system_files import VmConfig
from cloud_coder_vm.tmux_panes import no_server, tmux_command


class CloseError(Exception):
    pass


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def unpushed(rev: str, cwd: Path) -> bool:
    """Whether ``rev`` has commits that no remote-tracking branch has (or cannot be read)."""
    result = _git(["rev-list", "--max-count=1", rev, "--not", "--remotes"], cwd)
    return result.returncode != 0 or bool(result.stdout.strip())


def unsaved_work(workdir: Path) -> str | None:
    """What removing ``workdir`` would lose, or None when everything is on a remote.
    Ignored files count: `git worktree remove` deletes them, and they may be the only
    copy (a .env, a local database)."""
    status = _git(["status", "--porcelain", "--ignored"], workdir)
    if status.returncode != 0:
        return f"git status failed: {status.stderr.strip()}"
    lines = status.stdout.splitlines()
    ignored = [line[3:] for line in lines if line.startswith("!! ")]
    if len(ignored) < len(lines):
        return "uncommitted changes"
    if ignored:
        return "ignored files that would be deleted (" + ", ".join(ignored[:5]) + ")"
    if unpushed("HEAD", workdir):
        return "commits that are not pushed"
    return None


def kill_tmux_session(session_name: str) -> str:
    killed = subprocess.run(
        [*tmux_command(None), "kill-session", "-t", f"={session_name}"],
        capture_output=True,
        text=True,
    )
    if killed.returncode == 0:
        return "killed"
    if "can't find session" in killed.stderr or no_server(killed.stderr):
        return "absent"
    raise CloseError(f"tmux kill-session failed: {killed.stderr.strip()}")


def close(config: VmConfig, home: Path, session_name: str) -> dict:
    """The session is taken out of the registry first, so a `connect` that starts after
    that cannot restart it while it is being closed; it is put back if closing fails
    before anything is deleted. Its name is not given to a new session while its
    branch is left anywhere (see `launch.left_behind`)."""
    registry_file = paths.registry_path(home)
    sessions = session_registry.load(registry_file)
    if session_name not in sessions:
        raise CloseError(f"unknown session {session_name!r}")
    session = sessions[session_name]
    main = main_checkout(Layout.of(config, home), session.repo)
    workdir = Path(session.workdir)
    is_worktree = workdir != main and workdir.exists()
    reason = unsaved_work(workdir) if is_worktree else None
    if reason is not None:
        raise CloseError(
            f"{workdir} has {reason}; save or remove them, then close again (nothing was closed)"
        )
    with state_lock():
        sessions = session_registry.load(registry_file)
        if sessions.pop(session_name, None) is None:
            raise CloseError(f"unknown session {session_name!r}")
        session_registry.save(registry_file, sessions)

    try:
        result: dict = {"session": session_name, "tmux": kill_tmux_session(session_name)}
        if is_worktree:
            # again: Claude Code may have written until it was ended
            reason = unsaved_work(workdir)
            if reason is not None:
                raise CloseError(
                    f"{workdir} has {reason}; its tmux session was ended, the worktree "
                    "is kept: save or remove them, then close again"
                )
            removed = _git(["worktree", "remove", str(workdir)], main)
            if removed.returncode != 0:
                raise CloseError(f"git worktree remove failed: {removed.stderr.strip()}")
    except BaseException:
        with state_lock():
            sessions = session_registry.load(registry_file)
            sessions.setdefault(session_name, session)
            session_registry.save(registry_file, sessions)
        raise
    if is_worktree:
        result["worktree"] = "removed"
        branch = f"cloud-coder/{session_name}"
        if _git(["rev-parse", "--verify", "--quiet", branch], main).returncode == 0:
            if unpushed(branch, main):
                result["branch"] = f"kept: {branch} has commits that are not pushed"
            else:
                deleted = _git(["branch", "-D", branch], main)
                result["branch"] = (
                    "deleted"
                    if deleted.returncode == 0
                    else f"kept: git branch -D failed: {deleted.stderr.strip()}"
                )
    else:
        result["worktree"] = "kept (main checkout)" if workdir == main else "absent"
    return result
