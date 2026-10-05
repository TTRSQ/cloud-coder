"""Decide whether the whole VM is idle, and shut it down after a grace period.

Run every minute by a systemd timer. Each run rescans everything, so the
run that ends the grace period is also the final re-check before shutdown.
"""

import os
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from cloud_coder_vm import busy_markers, paths, process_table, session_state, ssh_logins, tmux_panes
from cloud_coder_vm.process_table import Process
from cloud_coder_vm.session_state import BUSY, IDLE, READY, SessionState
from cloud_coder_vm.state_lock import state_lock, write_atomic
from cloud_coder_vm.system_files import VmConfig
from cloud_coder_vm.tmux_panes import Pane


@dataclass
class Evaluation:
    idle: bool
    busy_reasons: list[str] = field(default_factory=list)
    live_states: list[SessionState] = field(default_factory=list)
    stale_states: list[SessionState] = field(default_factory=list)  # Claude Code gone


def _state_is_live(state: SessionState, procs: dict[int, Process], claude_panes: set[str]) -> bool:
    if state.claude_pid is not None:
        proc = procs.get(state.claude_pid)
        if proc is None or not process_table.is_claude(proc):
            return False
        return state.claude_starttime is None or proc.starttime == state.claude_starttime
    return state.tmux_pane in claude_panes


# A READY session (its last Stop reported no background tasks and no crons) that has
# seen no event for this long counts as idle even without Notification(idle_prompt).
# Claude Code 2.1.288 was observed not sending idle_prompt for a Remote Control session
# (while a phone was connected / a dialog was open), which left the VM up forever.
READY_IDLE_AFTER_SECONDS = 120


# A session that (re)started without a turn (resume, /clear, ...) is BUSY because a
# resume restores scheduled crons. Claude Code sends no idle_prompt then (observed on
# 2.1.288), so after this long without any event it counts as idle.
SESSION_START_IDLE_AFTER_SECONDS = 600


def is_idle_state(state: SessionState, now: float) -> bool:
    if state.state == IDLE:
        return True
    if state.state == BUSY and state.last_event == "SessionStart":
        return now - state.updated_at >= SESSION_START_IDLE_AFTER_SECONDS
    # Only a READY that came from Stop: Stop reported no background tasks and no crons.
    # A READY from StopFailure reported nothing, so it waits for idle_prompt.
    return (
        state.state == READY
        and state.last_event == "Stop"
        and now - state.updated_at >= READY_IDLE_AFTER_SECONDS
    )


def _claude_name(state: SessionState) -> str:
    name = state.cloud_coder_session or state.session_id
    return f"claude {name} ({state.tmux_pane or 'no tmux'})"


def _where(pane: Pane) -> str:
    where = f"{pane.session} {pane.pane_id}"
    return where if pane.socket == tmux_panes.SOCKET else f"{where} (tmux -L {pane.socket})"


def idle_reasons(ev: Evaluation, panes: list[Pane] | None, now: float) -> list[str]:
    """Why an idle VM counts as idle, for the log: each live Claude Code's state and
    what the tmux panes run."""
    reasons = [
        f"{_claude_name(s)} is {s.state} after {s.last_event} {now - s.updated_at:.0f}s ago"
        for s in ev.live_states
    ] or ["no Claude Code reports a state"]
    reasons.append(f"{len(panes or [])} tmux panes, all at a shell prompt or running claude")
    return reasons


def evaluate(
    panes: list[Pane] | None,
    procs: dict[int, Process],
    states: list[SessionState],
    now: float,
    containers: list[str] | None = (),
    logins: list[ssh_logins.Login] = (),
    ssh_idle_seconds: float = 0,
) -> Evaluation:
    """``containers``: names of running Docker containers, None if Docker could not be
    queried; pass () when Docker is absent or ignored."""
    if panes is None:
        return Evaluation(idle=False, busy_reasons=["tmux server could not be queried"])

    children = process_table.children_map(procs)
    subtrees = {pane: process_table.pane_subtree(pane.pane_pid, procs, children) for pane in panes}
    claude_panes = {pane.pane_id for pane, (claudes, _) in subtrees.items() if claudes}

    ev = Evaluation(idle=True)
    for state in states:
        if _state_is_live(state, procs, claude_panes):
            ev.live_states.append(state)
        else:
            ev.stale_states.append(state)

    registered_pids = {s.claude_pid for s in ev.live_states if s.claude_pid is not None}
    registered_panes = {s.tmux_pane for s in ev.live_states if s.claude_pid is None}

    for state in ev.live_states:
        if not is_idle_state(state, now):
            ev.busy_reasons.append(f"{_claude_name(state)} is {state.state}")

    for pane in panes:
        claudes, others = subtrees[pane]
        where = _where(pane)
        for proc in claudes:
            if proc.pid not in registered_pids and pane.pane_id not in registered_panes:
                ev.busy_reasons.append(f"{where}: claude pid {proc.pid} has not reported state yet")
        if not others:
            continue
        root, rest = others[0], others[1:]  # the pane process is always visited first
        if not process_table.is_shell(root):
            ev.busy_reasons.append(f"{where}: running {os.path.basename(root.argv0) or '?'}")
        elif rest:
            names = sorted({os.path.basename(p.argv0) or "?" for p in rest})
            ev.busy_reasons.append(f"{where}: shell has running processes {', '.join(names)}")

    ev.busy_reasons += ssh_logins.busy_reasons(logins, procs, now, ssh_idle_seconds)

    # Containers run outside tmux (docker compose up -d), so tmux cannot see them.
    if containers is None:
        ev.busy_reasons.append("docker could not be queried")
    elif containers:
        ev.busy_reasons.append(f"docker: running containers {', '.join(sorted(containers))}")

    ev.idle = not ev.busy_reasons
    return ev


@dataclass(frozen=True)
class Decision:
    action: str  # "busy" | "grace-started" | "grace" | "shutdown"
    idle_since: float | None
    remaining_seconds: float | None


def decide(idle: bool, idle_since: float | None, now: float, grace_seconds: float) -> Decision:
    if not idle:
        return Decision("busy", None, None)
    if idle_since is None:
        return Decision("grace-started", now, grace_seconds)
    remaining = grace_seconds - (now - idle_since)
    if remaining <= 0:
        return Decision("shutdown", idle_since, 0)
    return Decision("grace", idle_since, remaining)


def read_idle_since(path: Path = paths.IDLE_SINCE) -> float | None:
    try:
        return float(path.read_text().strip())
    except (OSError, ValueError):
        return None


def cancel_grace(path: Path = paths.IDLE_SINCE) -> None:
    """Called under the state lock whenever new work starts."""
    path.unlink(missing_ok=True)


SUBPROCESS_TIMEOUT = 20


def running_containers() -> list[str] | None:
    """Running container names; [] when Docker is not installed or its daemon is down,
    None when Docker could not be queried (counts as busy)."""
    if not Path("/usr/bin/docker").exists():
        return []
    try:
        active = subprocess.run(
            ["systemctl", "is-active", "--quiet", "docker"], timeout=SUBPROCESS_TIMEOUT
        )
        if active.returncode != 0:
            return []
        result = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.split()


@dataclass
class Observation:
    """Everything the idle decision needs that is slow to gather (no lock held)."""

    panes: list[Pane] | None
    procs: dict[int, Process]
    containers: list[str] | None
    logins: list[ssh_logins.Login]


def observe(config: VmConfig) -> Observation:
    return Observation(
        tmux_panes.list_all_panes(config.user, timeout=SUBPROCESS_TIMEOUT),
        process_table.snapshot(),
        () if config.ignore_docker else running_containers(),
        () if config.ignore_ssh_sessions else ssh_logins.read_logins(),
    )


def judge(config: VmConfig, obs: Observation) -> Evaluation:
    """Evaluate with the session states and work markers as they are right now."""
    ev = evaluate(
        obs.panes,
        obs.procs,
        session_state.load_all(paths.SESSION_STATE_DIR),
        time.time(),
        obs.containers,
        obs.logins,
        config.ssh_session_idle_minutes * 60,
    )
    for kind in busy_markers.active():
        ev.idle = False
        ev.busy_reasons.append(f"cloud-coder {kind} in progress")
    return ev


def scan(config: VmConfig) -> Evaluation:
    return judge(config, observe(config))


def run(
    config: VmConfig,
    shutdown: Callable[[], None] | None = None,
    now: Callable[[], float] = time.time,
) -> Decision:
    # tmux / docker / /proc are read without the lock; session states, markers and the
    # grace timestamp are re-read under it, so a prompt or launch that lands while we
    # look still cancels the decision.
    obs = observe(config)
    with state_lock():
        ev = judge(config, obs)
        for state in ev.stale_states:
            session_state.remove(paths.SESSION_STATE_DIR, state.key)
            print(f"removed the state of {_claude_name(state)}: its process is gone", flush=True)
        grace_seconds = config.grace_seconds
        at = now()
        decision = decide(ev.idle, read_idle_since(), at, grace_seconds)
        if decision.action == "busy":
            cancel_grace()
            print("busy: " + "; ".join(ev.busy_reasons), flush=True)
        elif decision.action == "grace-started":
            write_atomic(paths.IDLE_SINCE, f"{decision.idle_since}\n")
            print(
                f"idle: grace period of {grace_seconds:.0f}s started: "
                + "; ".join(idle_reasons(ev, obs.panes, at)),
                flush=True,
            )
        elif decision.action == "grace":
            print(f"idle: shutdown in {decision.remaining_seconds:.0f}s", flush=True)
        else:
            print(
                "idle: grace period over and still idle; shutting down: "
                + "; ".join(idle_reasons(ev, obs.panes, at)),
                flush=True,
            )
            (shutdown or _shutdown_now)()
        return decision


def _shutdown_now() -> None:
    subprocess.run(["/usr/sbin/shutdown", "-h", "now"], check=False)
