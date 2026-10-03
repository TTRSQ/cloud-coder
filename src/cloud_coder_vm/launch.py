"""Bring one cloud-coder session up: repo -> tmux session -> Claude Code with Remote Control.

Every step checks the current state first and only does what is missing,
so ``connect`` can be re-run at any point. Existing working trees are never
pulled or reset, and a pane where something is running is never typed into.
"""

import grp
import os
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from cloud_coder_vm import (
    busy_markers,
    idle_check,
    paths,
    process_table,
    session_registry,
    session_state,
    workspace_trust,
)
from cloud_coder_vm.session_registry import LogicalSession
from cloud_coder_vm.session_state import IDLE, READY
from cloud_coder_vm.state_lock import state_lock, write_atomic
from cloud_coder_vm.system_files import VmConfig
from cloud_coder_vm.tmux_panes import PANE_FORMAT, parse_list_panes


class LaunchError(Exception):
    pass


@dataclass(frozen=True)
class Target:
    session: LogicalSession
    created: bool


@dataclass(frozen=True)
class Layout:
    """Where cloud-coder puts repositories on the VM."""

    workspace: Path  # clones: <workspace>/<repo>
    worktrees: Path  # --new sessions: <worktrees>/<repo>-<n>
    legacy_workspace: Path | None = None  # older default; sessions there keep working

    @classmethod
    def of(cls, config: VmConfig, home: Path) -> "Layout":
        return cls(home / config.workspace, home / config.worktrees, home / paths.LEGACY_WORKSPACE)

    def trust_roots(self) -> list[Path]:
        roots = [self.workspace, self.worktrees]
        return roots + ([self.legacy_workspace] if self.legacy_workspace else [])


def main_checkout(layout: Layout, repo: str) -> Path:
    return layout.workspace / repo


def worktree_dir(layout: Layout, repo: str, index: int) -> Path:
    return layout.worktrees / f"{repo}-{index}"


def main_checkout_of(workdir: Path) -> Path:
    """The main checkout a worktree belongs to (Claude Code keys trust on it)."""
    result = _run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=workdir)
    if result.returncode != 0:
        return workdir
    return Path(result.stdout.strip()).parent


def resolve_target(
    sessions: dict[str, LogicalSession],
    layout: Layout,
    *,
    repo_url: str | None,
    repo: str | None,
    session_name: str | None,
    new: bool,
    now: float,
) -> Target:
    if session_name:
        if session_name not in sessions:
            raise LaunchError(f"unknown session {session_name!r}")
        return Target(sessions[session_name], created=False)
    if repo_url:
        repo = session_registry.repo_name_from_url(repo_url)
        known = {
            s.repo_url
            for s in sessions.values()
            if s.repo == repo
            and s.repo_url
            and session_registry.canonical_url(s.repo_url)
            != session_registry.canonical_url(repo_url)
        }
        if known:
            raise LaunchError(
                f"{repo!r} in the workspace was cloned from {sorted(known)[0]}, not {repo_url}"
            )
    if repo is None:
        if new:
            raise LaunchError("--new needs a repository")
        last = session_registry.latest(sessions)
        if last is None:
            raise LaunchError("no previous session; pass a repository URL")
        return Target(last, created=False)
    if not new:
        last = session_registry.latest(sessions, repo)
        if last is not None:
            return Target(last, created=False)
    index = session_registry.next_index(sessions, repo)
    workdir = main_checkout(layout, repo) if index == 1 else worktree_dir(layout, repo, index)
    known_url = repo_url or next((s.repo_url for s in sessions.values() if s.repo == repo), None)
    session = LogicalSession(
        name=session_registry.session_name(repo, index),
        repo=repo,
        repo_url=known_url,
        workdir=str(workdir),
        claude_session_id=str(uuid.uuid4()),
        created_at=now,
        last_connected_at=now,
    )
    return Target(session, created=True)


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _check(args: list[str], cwd: Path | None = None) -> str:
    result = _run(args, cwd)
    if result.returncode != 0:
        raise LaunchError(f"{shlex.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def ensure_repo(session: LogicalSession, layout: Layout, created: bool = False) -> str:
    """Clone / add a worktree only when the directory is missing. Returns what was done."""
    main = main_checkout(layout, session.repo)
    workdir = Path(session.workdir)
    if workdir.exists():
        if created and workdir != main:
            raise LaunchError(f"{workdir} already exists and was not created by cloud-coder")
        return "existing"
    actions = []
    if not main.exists():
        if not session.repo_url:
            raise LaunchError(f"{main} does not exist; pass the repository URL to clone it")
        env_ssh = "ssh -o StrictHostKeyChecking=accept-new"
        result = subprocess.run(
            ["git", "clone", "--", session.repo_url, str(main)],
            env={**os.environ, "GIT_SSH_COMMAND": env_ssh},
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise LaunchError(f"git clone {session.repo_url} failed: {result.stderr.strip()}")
        actions.append("cloned")
    if workdir != main and not workdir.exists():
        branch = f"cloud-coder/{session.name}"
        has_branch = _run(["git", "rev-parse", "--verify", "--quiet", branch], cwd=main)
        if has_branch.returncode == 0:
            _check(["git", "worktree", "add", str(workdir), branch], cwd=main)
        else:
            _check(["git", "worktree", "add", "-b", branch, str(workdir)], cwd=main)
        actions.append("worktree-added")
    return ",".join(actions) or "existing"


def _tmux_server_groups() -> set[int] | None:
    sessions = _run(["tmux", "list-sessions", "-F", "#{session_name}"]).stdout.split()
    if not sessions:
        return None  # no server yet: the new one inherits this login's groups
    pid = _run(["tmux", "display-message", "-p", "-t", f"={sessions[0]}", "#{pid}"]).stdout
    try:
        status = Path(f"/proc/{pid.strip()}/status").read_text()
    except OSError:
        return None
    line = next((x for x in status.splitlines() if x.startswith("Groups:")), "Groups:")
    return {int(g) for g in line.split()[1:]}


def regroup_command(
    group: str, login_groups: set[int], server_groups: set[int] | None
) -> list[str]:
    """Shell for a new pane that has ``group`` although the tmux server predates it.

    usermod -aG only affects new logins, and panes inherit the tmux server's groups.
    """
    try:
        gid = grp.getgrnam(group).gr_gid
    except KeyError:
        return []
    if server_groups is None or gid in server_groups or gid not in login_groups:
        return []
    return ["sg", group, "-c", 'exec "${SHELL:-/bin/bash}" -l']


def ensure_tmux_session(session: LogicalSession) -> str:
    if _run(["tmux", "has-session", "-t", f"={session.name}"]).returncode == 0:
        return "existing"
    shell = regroup_command("docker", set(os.getgroups()), _tmux_server_groups())
    _check(
        [
            "tmux",
            "new-session",
            "-d",
            "-s",
            session.name,
            "-c",
            session.workdir,
            "-e",
            f"CLOUD_CODER_SESSION={session.name}",
            *shell,
        ]
    )
    return "created"


def transcript_exists(home: Path, claude_session_id: str) -> bool:
    return any((home / ".claude/projects").glob(f"*/{claude_session_id}.jsonl"))


def claude_command(
    home: Path, session: LogicalSession, resume: bool, prompt_file: Path | None = None
) -> str:
    """Shell command typed into the pane. The first prompt is read from a file so that
    multi-line text and shell metacharacters reach Claude Code unchanged."""
    id_flag = "--resume" if resume else "--session-id"
    command = shlex.join(
        [
            f"CLOUD_CODER_SESSION={session.name}",
            str(paths.claude_bin(home)),
            id_flag,
            session.claude_session_id,
            "--remote-control",
            session.name,
        ]
    )
    if prompt_file is None:
        return command
    quoted = shlex.quote(str(prompt_file))
    return f'{command} -- "$(cat {quoted}; rm -f {quoted})"'


def write_prompt_file(session: LogicalSession, prompt: str) -> Path:
    path = paths.PROMPT_DIR / f"{session.name}.txt"
    write_atomic(path, prompt, mode=0o600)
    return path


def send_prompt_to_running(pane_id: str, claude_pid: int, prompt: str) -> None:
    """Type a prompt into a running Claude Code, only when it is waiting for input."""
    states = session_state.load_all(paths.SESSION_STATE_DIR)
    state = next(
        (
            s
            for s in states
            if s.claude_pid == claude_pid or (s.claude_pid is None and s.tmux_pane == pane_id)
        ),
        None,
    )
    if state is None:
        raise LaunchError(
            "Claude Code in this session has not reported its state yet; prompt not sent"
        )
    if state.state not in (READY, IDLE):
        raise LaunchError(f"Claude Code in this session is {state.state}; prompt not sent")
    buffer = f"cloud-coder-{os.getpid()}"
    subprocess.run(["tmux", "load-buffer", "-b", buffer, "-"], input=prompt, text=True, check=True)
    # Bracketed paste (-p) keeps newlines inside the prompt instead of submitting each line.
    _check(["tmux", "paste-buffer", "-p", "-d", "-b", buffer, "-t", pane_id])
    time.sleep(0.3)
    _check(["tmux", "send-keys", "-t", pane_id, "Enter"])


def _cmdline(pid: int) -> list[str]:
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [a.decode(errors="replace") for a in raw.split(b"\0") if a]


def ensure_claude(
    session: LogicalSession, home: Path, prompt: str | None = None, reuse_pane: bool = False
) -> dict:
    """Start Claude Code in the session unless one already runs there; then hand it ``prompt``."""
    panes = parse_list_panes(
        _check(["tmux", "list-panes", "-s", "-t", f"={session.name}", "-F", PANE_FORMAT])
    )
    procs = process_table.snapshot()
    children = process_table.children_map(procs)

    free_pane = None
    for pane in panes:
        claudes, others = process_table.pane_subtree(pane.pane_pid, procs, children)
        for proc in claudes:
            remote = "--remote-control" in _cmdline(proc.pid) or "--rc" in _cmdline(proc.pid)
            result = {"claude": "running", "remote_control": remote, "pane": pane.pane_id}
            if prompt is not None:
                send_prompt_to_running(pane.pane_id, proc.pid, prompt)
                result["prompt"] = "sent"
            return result
        if (
            reuse_pane
            and free_pane is None
            and len(others) == 1
            and process_table.is_shell(others[0])
        ):
            free_pane = pane.pane_id

    if free_pane is None:
        free_pane = _check(
            [
                "tmux",
                "new-window",
                "-t",
                f"={session.name}:",
                "-c",
                session.workdir,
                "-P",
                "-F",
                "#{pane_id}",
                *regroup_command("docker", set(os.getgroups()), _tmux_server_groups()),
            ]
        ).strip()
    resume = transcript_exists(home, session.claude_session_id)
    prompt_file = write_prompt_file(session, prompt) if prompt is not None else None
    command = claude_command(home, session, resume, prompt_file)
    _check(["tmux", "send-keys", "-t", free_pane, "-l", command])
    _check(["tmux", "send-keys", "-t", free_pane, "Enter"])
    result = {
        "claude": "resumed" if resume else "started",
        "remote_control": True,
        "pane": free_pane,
    }
    if prompt is not None:
        result["prompt"] = "passed-at-start"
    return result


def launch(
    config: VmConfig,
    home: Path,
    *,
    repo_url: str | None = None,
    repo: str | None = None,
    session_name: str | None = None,
    new: bool = False,
    start_claude: bool = True,
    prompt: str | None = None,
) -> dict:
    if prompt is not None and not prompt.strip():
        raise LaunchError("the prompt is empty")
    if prompt is not None and not start_claude:
        raise LaunchError("a prompt needs Claude Code; drop --no-claude")
    layout = Layout.of(config, home)
    registry_file = paths.registry_path(home)
    # The marker keeps the idle check from stopping the VM while we clone / start
    # Claude Code; the state lock is held only for the short registry updates so
    # hooks of other sessions are never kept waiting.
    with busy_markers.busy_marker("launch"):
        with state_lock():
            idle_check.cancel_grace()
            sessions = session_registry.load(registry_file)
            target = resolve_target(
                sessions,
                layout,
                repo_url=repo_url,
                repo=repo,
                session_name=session_name,
                new=new,
                now=time.time(),
            )
            session = target.session
            session.last_connected_at = time.time()
            sessions[session.name] = session  # reserves the name for concurrent --new
            session_registry.save(registry_file, sessions)
        result: dict = {
            "session": session.name,
            "workdir": session.workdir,
            "claude_session_id": session.claude_session_id,
            "created": target.created,
        }
        try:
            result["repo"] = ensure_repo(session, layout, target.created)
            if config.auto_trust_workspace:
                workdir = Path(session.workdir)
                trusted = {main_checkout_of(workdir), workdir}
                try:
                    result["trust_written"] = workspace_trust.trust(
                        paths.claude_global_config_path(home), sorted(trusted), layout.trust_roots()
                    )
                except ValueError as e:
                    raise LaunchError(str(e)) from e
        except LaunchError:
            if target.created:
                with state_lock():
                    sessions = session_registry.load(registry_file)
                    sessions.pop(session.name, None)
                    session_registry.save(registry_file, sessions)
            raise
        result["tmux"] = ensure_tmux_session(session)
        if start_claude:
            # A pane of an existing tmux session may hold half-typed input: only the
            # pane of a session created just now is typed into, otherwise a new window.
            reuse_pane = result["tmux"] == "created"
            result.update(ensure_claude(session, home, prompt, reuse_pane))
    return result
