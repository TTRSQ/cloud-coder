"""Bring one cloud-coder session up: repo -> tmux session -> Claude Code with Remote Control.

Every step checks the current state first and only does what is missing,
so ``connect`` can be re-run at any point. Existing working trees are never
pulled or reset, and a pane where something other than Claude Code runs is never typed into.
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
from cloud_coder_vm.tmux_panes import DEFAULT_SOCKET, PANE_FORMAT, parse_list_panes, tmux_command


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
    legacy_workspace: Path  # the older default; clones there keep being used

    @classmethod
    def of(cls, config: VmConfig, home: Path) -> "Layout":
        return cls(home / config.workspace, home / config.worktrees, home / paths.LEGACY_WORKSPACE)

    def trust_roots(self) -> list[Path]:
        return list(dict.fromkeys([self.workspace, self.worktrees, self.legacy_workspace]))


def main_checkout(layout: Layout, repo: str) -> Path:
    """The one clone of ``repo`` that sessions and worktrees use. A clone under the legacy
    workspace wins, since cloud-coder no longer creates clones there. Whatever this
    returns is checked against the session's URL before it is built on or trusted."""
    legacy = layout.legacy_workspace / repo
    return legacy if legacy.is_dir() else layout.workspace / repo


def worktree_dir(layout: Layout, repo: str, index: int) -> Path:
    return layout.worktrees / f"{repo}-{index}"


def resolve_target(
    sessions: dict[str, LogicalSession],
    layout: Layout,
    *,
    repo_url: str | None,
    repo: str | None,
    session_name: str | None,
    new: bool,
    has_prompt: bool,
    now: float,
) -> Target:
    """The session to connect to. A named session is continued. Otherwise a prompt is a
    new task: it gets a new session and conversation, never an earlier one; without a
    prompt (someone attaching to look) the repository's latest session is returned to,
    unless ``new`` asks for another one."""
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
        if has_prompt:
            raise LaunchError(
                "a prompt without a session starts a new session, which needs a repository; "
                "name the session to continue it"
            )
        if new:
            raise LaunchError("--new needs a repository")
        last = session_registry.latest(sessions)
        if last is None:
            raise LaunchError("no previous session; pass a repository URL")
        return Target(last, created=False)
    if not (new or has_prompt):
        last = session_registry.latest(sessions, repo)
        if last is not None:
            return Target(last, created=False)
    index = session_registry.next_index(
        sessions, repo, lambda index: left_behind(layout, repo, index)
    )
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


def left_behind(layout: Layout, repo: str, index: int) -> bool:
    """Whether a closed session left the worktree or branch of session ``index`` behind
    (its branch is usually pushed, often with a pull request): a new session must not
    reuse that name and start a new task on the old work."""
    if index == 1:
        return False  # the main checkout outlives its sessions by design
    if worktree_dir(layout, repo, index).exists():
        return True
    main = main_checkout(layout, repo)
    if not main.exists():
        return False
    branch = f"cloud-coder/{session_registry.session_name(repo, index)}"
    refs = _run(
        ["git", "for-each-ref", "--count=1", f"refs/heads/{branch}", f"refs/remotes/*/{branch}"],
        cwd=main,
    )
    return bool(refs.stdout.strip())  # a broken clone fails in ensure_repo instead


def _run(args: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True)


def _check(args: list[str], cwd: Path | None = None) -> str:
    result = _run(args, cwd)
    if result.returncode != 0:
        raise LaunchError(f"{shlex.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def is_clone_of(clone: Path, repo_url: str | None) -> bool:
    """Whether ``clone`` is a clone of ``repo_url`` (whatever the transport)."""
    if repo_url is None:
        return False
    origin = _run(["git", "remote", "get-url", "origin"], cwd=clone).stdout.strip()
    return bool(origin) and (
        session_registry.canonical_url(origin) == session_registry.canonical_url(repo_url)
    )


def ensure_repo(session: LogicalSession, layout: Layout, created: bool = False) -> str:
    """Clone / add a worktree only when the directory is missing. Returns what was done.

    A clone cloud-coder builds on without making it now (an existing clone for a new
    session, or the source of a new worktree) must be of the session's repository;
    with only a repo name (``connect <name>``) the user vouches for the directory."""
    main = main_checkout(layout, session.repo)
    workdir = Path(session.workdir)
    if created and workdir != main and workdir.exists():
        raise LaunchError(f"{workdir} already exists and was not created by cloud-coder")
    builds_on_main = main.exists() and (created or not workdir.exists())
    if builds_on_main and session.repo_url and not is_clone_of(main, session.repo_url):
        raise LaunchError(
            f"{main} is not a clone of {session.repo_url}; move it away or connect with its URL"
        )
    if workdir.exists():
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
    sessions = _run([*tmux_command(), "list-sessions", "-F", "#{session_name}"]).stdout.split()
    if not sessions:
        return None  # no server yet: the new one inherits this login's groups
    pid = _run([*tmux_command(), "display-message", "-p", "-t", f"={sessions[0]}", "#{pid}"]).stdout
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
    if _run([*tmux_command(), "has-session", "-t", f"={session.name}"]).returncode == 0:
        return "existing"
    has_legacy = [*tmux_command(socket=DEFAULT_SOCKET), "has-session", "-t", f"={session.name}"]
    if _run(has_legacy).returncode == 0:
        # A second Claude Code would resume the conversation the old one still runs.
        raise LaunchError(
            f"{session.name} still runs on the default tmux server (started by an older "
            f"cloud-coder); end it with `tmux -L default kill-session -t ={session.name}` "
            "on the VM and connect again"
        )
    shell = regroup_command("docker", set(os.getgroups()), _tmux_server_groups())
    _check(
        [
            *tmux_command(),
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
    multi-line text and shell metacharacters reach Claude Code unchanged.

    Claude Code runs without $TMUX, so tmux run by its tools (a test that calls
    `tmux kill-server`, say) cannot reach cloud-coder's server. $TMUX_PANE stays: the
    hook keys the session state by it."""
    id_flag = "--resume" if resume else "--session-id"
    command = shlex.join(
        [
            "env",
            "-u",
            "TMUX",
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


# How long Claude Code has to confirm, through its UserPromptSubmit hook, a typed prompt.
PROMPT_CONFIRM_SECONDS = 10.0


def _reported_state(pane_id: str, claude_pid: int) -> session_state.SessionState | None:
    return next(
        (
            s
            for s in session_state.load_all(paths.SESSION_STATE_DIR)
            if s.claude_pid == claude_pid or (s.claude_pid is None and s.tmux_pane == pane_id)
        ),
        None,
    )


def _typeable_state(pane_id: str, claude_pid: int) -> session_state.SessionState:
    """The state of a Claude Code that a prompt may be typed into: one that reported its
    state and shows no dialog (Enter would answer the dialog, and the text would be lost)."""
    state = _reported_state(pane_id, claude_pid)
    if state is None:
        raise LaunchError(
            "Claude Code in this session has not reported its state yet; prompt not sent"
        )
    if state.open_dialogs:
        raise LaunchError(
            "Claude Code in this session shows a permission prompt or a question, or one "
            "was dismissed and Claude Code has not finished its work since; prompt not sent"
        )
    return state


def send_prompt_to_running(pane_id: str, claude_pid: int, prompt: str) -> str:
    """Type a prompt into a running Claude Code and submit it. Returns ``"sent"`` when
    Claude Code was waiting for input, ``"queued"`` when it was working (BUSY): Claude
    Code then queues the prompt itself and takes it in at its next step, in the same
    conversation, without interrupting the work. Raises LaunchError unless Claude Code
    confirms, through its UserPromptSubmit hook, that it received the prompt."""
    paths.PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    # One prompt at a time per Claude Code: interleaved pastes would merge into one prompt.
    with state_lock(paths.PROMPT_DIR / f"claude-{claude_pid}.lock"):
        before = _typeable_state(pane_id, claude_pid)
        buffer = f"cloud-coder-{os.getpid()}"
        subprocess.run(
            [*tmux_command(), "load-buffer", "-b", buffer, "-"],
            input=prompt,
            text=True,
            check=True,
        )
        # Bracketed paste (-p) keeps newlines inside the prompt instead of submitting each line.
        _check([*tmux_command(), "paste-buffer", "-p", "-d", "-b", buffer, "-t", pane_id])
        time.sleep(0.3)
        typed = _reported_state(pane_id, claude_pid)
        if typed is None or typed.open_dialogs:
            raise LaunchError(
                "Claude Code in this session showed a dialog or ended while the prompt was "
                "typed; prompt not submitted, and it may be left in the input box. Check the "
                "session's screen"
            )
        _check([*tmux_command(), "send-keys", "-t", pane_id, "Enter"])
        deadline = time.monotonic() + PROMPT_CONFIRM_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.2)
            now = _reported_state(pane_id, claude_pid)
            if now is not None and now.prompts_submitted > before.prompts_submitted:
                return "sent" if before.state in (READY, IDLE) else "queued"
    raise LaunchError(
        "Claude Code in this session did not confirm receiving the prompt within "
        f"{PROMPT_CONFIRM_SECONDS:.0f} s; prompt not confirmed, and it may be left in the "
        "input box or lost. Check the session's screen before sending it again"
    )


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
        _check([*tmux_command(), "list-panes", "-s", "-t", f"={session.name}", "-F", PANE_FORMAT])
    )
    procs = process_table.snapshot()
    children = process_table.children_map(procs)

    free_pane = None
    for pane in panes:
        claudes, others = process_table.pane_subtree(pane.pane_pid, procs, children)
        for proc in claudes:
            remote = "--remote-control" in _cmdline(proc.pid) or "--rc" in _cmdline(proc.pid)
            result = {
                "claude": "running",
                "conversation": "continued",
                "remote_control": remote,
                "pane": pane.pane_id,
            }
            if prompt is not None:
                result["prompt"] = send_prompt_to_running(pane.pane_id, proc.pid, prompt)
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
                *tmux_command(),
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
    _check([*tmux_command(), "send-keys", "-t", free_pane, "-l", command])
    _check([*tmux_command(), "send-keys", "-t", free_pane, "Enter"])
    result = {
        "claude": "resumed" if resume else "started",
        "conversation": "continued" if resume else "new",
        "remote_control": True,
        "pane": free_pane,
    }
    if prompt is not None:
        result["prompt"] = "passed-at-start"
    return result


def trust_session(session: LogicalSession, layout: Layout, home: Path) -> bool | str:
    """Pre-accept Claude Code's trust dialog for the session's checkout, but only when it
    is verified to be a clone of the session's URL. A session known only by repo name,
    or a clone whose origin changed, is left to Claude Code's own trust dialog.
    Returns whether ~/.claude.json was written, or why trust was skipped."""
    main = main_checkout(layout, session.repo)
    if not is_clone_of(main, session.repo_url):
        return "skipped: not verified as a clone of the session's URL"
    try:
        return workspace_trust.trust(
            paths.claude_global_config_path(home),
            sorted({main, Path(session.workdir)}),
            layout.trust_roots(),
        )
    except ValueError as e:
        raise LaunchError(str(e)) from e


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
                has_prompt=prompt is not None,
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
                result["trust_written"] = trust_session(session, layout, home)
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
