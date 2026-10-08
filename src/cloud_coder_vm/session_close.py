"""Close a cloud-coder session: end its tmux session, remove its worktree and forget it.

Work that exists only on the VM is never deleted. A worktree is kept, and the session is
not closed, while it has uncommitted changes, commits not on a remote, ignored files
that are not regenerable caches (see `regenerable_caches`), a mount point, or a process
other than the session's own using it. Only the caches are deleted with the worktree;
`discard_ignored` also deletes the other ignored files, never uncommitted or unpushed
work. `dry_run` reports all of this without changing anything. The main checkout is
never removed, and Claude Code's transcript stays on the disk.
"""

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from cloud_coder_vm import paths, process_table, regenerable_caches, session_registry
from cloud_coder_vm.launch import Layout, main_checkout
from cloud_coder_vm.state_lock import state_lock
from cloud_coder_vm.system_files import VmConfig
from cloud_coder_vm.tmux_panes import (
    DEFAULT_SOCKET,
    PANE_FORMAT,
    no_server,
    parse_list_panes,
    tmux_command,
)

# how long processes using the worktree get to exit after the tmux session is ended
EXIT_WAIT_SECONDS = 15.0
# blockers listed in an error message (a dry run lists them all)
LISTED_BLOCKERS = 10


class CloseError(Exception):
    pass


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="surrogateescape",
    )


def unpushed(rev: str, cwd: Path) -> bool:
    """Whether ``rev`` has commits that no remote-tracking branch has (or cannot be read)."""
    result = _git(["rev-list", "--max-count=1", rev, "--not", "--remotes"], cwd)
    return result.returncode != 0 or bool(result.stdout.strip())


def parse_status(output: str) -> tuple[list[str], list[str]]:
    """(changed, ignored) paths of `git status --porcelain=v1 -z --ignored=matching`:
    what an ignore pattern matched, a directory as one path ending in "/". (A directory
    whose content is all ignored is listed by that content, not as itself.)"""
    fields = output.split("\0")
    changed, ignored = [], []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if not entry:
            continue
        code, path = entry[:2], entry[3:]
        if code == "!!":
            ignored.append(path)
            continue
        changed.append(path)
        if "R" in code or "C" in code:
            i += 1  # the path it was renamed or copied from
    return changed, ignored


MOUNTINFO = Path("/proc/self/mountinfo")


def mount_points_under(directory: Path, mountinfo: Path = MOUNTINFO) -> list[str]:
    """Mount points at or under ``directory``: removing the worktree would delete what
    is mounted there."""
    resolved = os.path.realpath(directory)
    found = []
    for line in mountinfo.read_bytes().splitlines():
        fields = line.split(b" ")
        if len(fields) < 5:
            continue
        # a space in a mount point is written \040, and so on
        point = os.fsdecode(re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), fields[4]))
        if point == resolved or point.startswith(resolved + "/"):
            found.append(point)
    return found


@dataclass
class Inspection:
    """What closing a worktree session would do with its worktree."""

    blockers: list[str] = field(default_factory=list)  # each one stops the close
    caches: list[str] = field(default_factory=list)  # ignored, deleted as regenerable
    discarded: list[str] = field(default_factory=list)  # other ignored: discard_ignored


def inspect(workdir: Path, discard_ignored: bool, ended_pids: set[int]) -> Inspection:
    """Paths are relative to ``workdir``, as git lists them. Processes in
    ``ended_pids`` are ended with the session's tmux session, so they do not block."""
    found = Inspection()
    status = _git(
        ["status", "--porcelain=v1", "-z", "--untracked-files=normal", "--ignored=matching"],
        workdir,
    )
    if status.returncode != 0:
        found.blockers.append(f"git status failed: {status.stderr.strip()}")
        return found
    changed, ignored = parse_status(status.stdout)
    found.blockers += [f"uncommitted or untracked: {path}" for path in changed]
    for path in dict.fromkeys(judged_path(p, workdir) for p in ignored):
        reason = regenerable_caches.why_not_a_cache(workdir / path)
        if reason is None:
            found.caches.append(path)
        elif not discard_ignored:
            found.blockers.append(f"ignored file: {path} ({reason})")
        elif repository := git_repository_in(workdir / path):
            # its commits may be on no remote
            found.blockers.append(f"git repository in an ignored path: {repository}")
        else:
            found.discarded.append(path)
    if unpushed("HEAD", workdir):
        found.blockers.append("commits that are not on any remote")
    found.blockers += [f"mount point: {point}" for point in mount_points_under(workdir)]
    found.blockers += [
        f"process {proc.pid} ({os.path.basename(proc.argv0)}) uses the worktree"
        for proc in process_table.processes_using(workdir)
        if proc.pid not in ended_pids
    ]
    return found


def judged_path(ignored: str, workdir: Path) -> str:
    """The path to judge for an ignored path git listed: the cache directory around it
    (see `regenerable_caches.cache_root`) unless git tracks a file in that directory,
    which deleting it would take along."""
    root = regenerable_caches.cache_root(ignored)
    if root == ignored:
        return ignored
    tracked = _git(["ls-files", "-z", "--", root], workdir)
    return root if tracked.returncode == 0 and not tracked.stdout else ignored


def git_repository_in(path: Path) -> str | None:
    """A .git (a repository or a worktree's link to one) at or under ``path``."""
    if path.is_symlink():
        return None
    for root, dirs, files in os.walk(path):
        if ".git" in dirs or ".git" in files:
            return os.path.join(root, ".git")
    return None


def delete_ignored(workdir: Path, relative_paths: list[str]) -> None:
    """Delete these ignored paths of ``workdir`` without following symlinks: a directory
    with everything in it, a file or a symlink itself."""
    root = os.path.realpath(workdir)
    for relative in relative_paths:
        path = workdir / relative.rstrip("/")
        parent = os.path.realpath(path.parent)
        if parent != root and not parent.startswith(root + "/"):
            raise CloseError(f"{relative} is not inside {workdir}")
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        except OSError as e:
            raise CloseError(f"deleting {path} failed: {e}") from e


def session_pids(session_name: str) -> set[int]:
    """Processes in the session's tmux panes, which ending the tmux session ends."""
    listed = subprocess.run(
        [*tmux_command(None), "list-panes", "-s", "-t", f"={session_name}", "-F", PANE_FORMAT],
        capture_output=True,
        text=True,
    )
    panes = parse_list_panes(listed.stdout) if listed.returncode == 0 else []
    procs = process_table.snapshot()
    children = process_table.children_map(procs)
    pids: set[int] = set()
    stack = [pane.pane_pid for pane in panes]
    while stack:
        pid = stack.pop()
        if pid not in pids:
            pids.add(pid)
            stack.extend(children.get(pid, []))
    return pids


def wait_until_unused(workdir: Path) -> None:
    deadline = time.monotonic() + EXIT_WAIT_SECONDS
    while True:
        users = process_table.processes_using(workdir)
        if not users:
            return
        if time.monotonic() >= deadline:
            listed = ", ".join(f"{p.pid} ({os.path.basename(p.argv0)})" for p in users)
            raise CloseError(
                f"processes still use {workdir} after its tmux session was ended: {listed}; "
                "the worktree is kept: end them, then close again"
            )
        time.sleep(0.5)


def refusal(workdir: Path, blockers: list[str], session_name: str) -> str:
    listed = "; ".join(blockers[:LISTED_BLOCKERS])
    if len(blockers) > LISTED_BLOCKERS:
        listed += f"; and {len(blockers) - LISTED_BLOCKERS} more"
    # no pointer to --discard-ignored: an ignored file may be the only copy of
    # something (.env, research output); deleting it is the user's decision
    hint = f"`cloud-coder close {session_name} --dry-run` lists them all"
    return f"{workdir} has {listed}; nothing was closed ({hint})"


def runs_on_default_server(session_name: str) -> bool:
    """Whether an older cloud-coder left the session on the default tmux server, where
    `kill_tmux_session` does not reach it. As there, only a session tmux does not know
    counts as absent."""
    has_session = [*tmux_command(None, DEFAULT_SOCKET), "has-session", "-t", f"={session_name}"]
    found = subprocess.run(has_session, capture_output=True, text=True)
    if found.returncode == 0:
        return True
    if "can't find session" in found.stderr or no_server(found.stderr):
        return False
    raise CloseError(f"tmux has-session (default server) failed: {found.stderr.strip()}")


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


def close(
    config: VmConfig,
    home: Path,
    session_name: str,
    dry_run: bool = False,
    discard_ignored: bool = False,
) -> dict:
    """The session is taken out of the registry first, so a `connect` that starts after
    that cannot restart it while it is being closed; it is put back if closing fails
    before the worktree is removed. Its name is not given to a new session while its
    branch is left anywhere (see `launch.left_behind`).

    After its tmux session is ended, the worktree is checked again, its caches (and with
    ``discard_ignored`` its other ignored files) are deleted, and `git worktree remove`
    (never with --force) removes it only when nothing else is left in it."""
    registry_file = paths.registry_path(home)
    sessions = session_registry.load(registry_file)
    if session_name not in sessions:
        raise CloseError(f"unknown session {session_name!r}")
    session = sessions[session_name]
    if runs_on_default_server(session_name):
        # its Claude Code would keep running in a worktree removed under it
        raise CloseError(
            f"{session_name} still runs on the default tmux server (started by an older "
            f"cloud-coder); end it with `tmux -L default kill-session -t ={session_name}` "
            "on the VM and close again (nothing was closed)"
        )
    main = main_checkout(Layout.of(config, home), session.repo)
    workdir = Path(session.workdir)
    is_worktree = workdir != main and workdir.exists()
    own_pids = session_pids(session_name)
    found = inspect(workdir, discard_ignored, own_pids) if is_worktree else Inspection()
    if dry_run:
        return preview(session_name, workdir, main, is_worktree, found, own_pids)
    if found.blockers:
        raise CloseError(refusal(workdir, found.blockers, session_name))
    with state_lock():
        sessions = session_registry.load(registry_file)
        if sessions.pop(session_name, None) is None:
            raise CloseError(f"unknown session {session_name!r}")
        session_registry.save(registry_file, sessions)

    try:
        result: dict = {"session": session_name, "tmux": kill_tmux_session(session_name)}
        if is_worktree:
            wait_until_unused(workdir)
            # again: Claude Code may have written until it was ended
            found = inspect(workdir, discard_ignored, set())
            if found.blockers:
                raise CloseError(
                    f"{workdir} has " + "; ".join(found.blockers[:LISTED_BLOCKERS]) + "; its "
                    "tmux session was ended, the worktree is kept: save or remove them, then "
                    "close again"
                )
            delete_ignored(workdir, found.caches + found.discarded)
            # nothing may be left for `git worktree remove` to delete but committed files
            left = inspect(workdir, False, set())
            if left.blockers or left.caches:
                raise CloseError(
                    f"{workdir} has "
                    + "; ".join(left.blockers[:LISTED_BLOCKERS] or left.caches)
                    + "; its tmux session was ended and its caches deleted, the worktree is "
                    "kept: save or remove them, then close again"
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
        result["deleted_caches"] = found.caches
        if discard_ignored:
            result["discarded_ignored"] = found.discarded
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


def preview(
    session_name: str,
    workdir: Path,
    main: Path,
    is_worktree: bool,
    found: Inspection,
    own_pids: set[int],
) -> dict:
    """What `close` would do, without doing it. Sizes are what deleting frees on the
    disk: per cache, and for the whole worktree (what closing it frees)."""
    if is_worktree:
        worktree = str(workdir)
    else:
        worktree = "kept (main checkout)" if workdir == main else "absent"
    procs = process_table.snapshot()
    caches = [
        {"path": path, "bytes": regenerable_caches.disk_usage(workdir / path)}
        for path in found.caches
    ]
    out: dict = {
        "session": session_name,
        "dry_run": True,
        "closable": not found.blockers,
        "worktree": worktree,
        "tmux": "running, ended by close" if own_pids else "absent",
        # what ending the tmux session stops besides Claude Code and the shells
        "ended_processes": [
            f"{pid} ({os.path.basename(procs[pid].argv0)})"
            for pid in sorted(own_pids)
            if pid in procs
            and not process_table.is_shell(procs[pid])
            and not process_table.is_claude(procs[pid])
            and not process_table.is_wrapper(procs[pid])
        ],
        "blockers": found.blockers,
        "caches": caches,
        "caches_bytes": sum(cache["bytes"] for cache in caches),
    }
    if found.discarded:
        out["discarded_ignored"] = found.discarded
    size = regenerable_caches.disk_usage(workdir) if is_worktree else 0
    out["worktree_bytes"] = size
    out["worktree_size"] = regenerable_caches.human_size(size)
    return out
