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

from cloud_coder_vm import paths, process_table, session_state, tmux_panes
from cloud_coder_vm.process_table import Process
from cloud_coder_vm.session_state import IDLE, SessionState
from cloud_coder_vm.state_lock import state_lock, write_atomic
from cloud_coder_vm.tmux_panes import Pane


@dataclass
class Evaluation:
    idle: bool
    busy_reasons: list[str] = field(default_factory=list)
    live_states: list[SessionState] = field(default_factory=list)
    stale_keys: list[str] = field(default_factory=list)


def _state_is_live(state: SessionState, procs: dict[int, Process], claude_panes: set[str]) -> bool:
    if state.claude_pid is not None:
        proc = procs.get(state.claude_pid)
        if proc is None or not process_table.is_claude(proc):
            return False
        return state.claude_starttime is None or proc.starttime == state.claude_starttime
    return state.tmux_pane in claude_panes


def _pane_subtree(pane: Pane, procs, children) -> tuple[list[Process], list[Process]]:
    """Return (claude processes, other processes) under the pane, not descending into Claude."""
    claudes, others = [], []
    stack = [pane.pane_pid]
    while stack:
        proc = procs.get(stack.pop())
        if proc is None:
            continue
        if process_table.is_claude(proc):
            claudes.append(proc)
            continue
        others.append(proc)
        stack.extend(children.get(proc.pid, []))
    return claudes, others


def evaluate(
    panes: list[Pane] | None,
    procs: dict[int, Process],
    states: list[SessionState],
) -> Evaluation:
    if panes is None:
        return Evaluation(idle=False, busy_reasons=["tmux server could not be queried"])

    children = process_table.children_map(procs)
    subtrees = {pane.pane_id: _pane_subtree(pane, procs, children) for pane in panes}
    claude_panes = {pane_id for pane_id, (claudes, _) in subtrees.items() if claudes}

    ev = Evaluation(idle=True)
    for state in states:
        if _state_is_live(state, procs, claude_panes):
            ev.live_states.append(state)
        else:
            ev.stale_keys.append(state.key)

    registered_pids = {s.claude_pid for s in ev.live_states if s.claude_pid is not None}
    registered_panes = {s.tmux_pane for s in ev.live_states if s.claude_pid is None}

    for state in ev.live_states:
        if state.state != IDLE:
            name = state.cloud_coder_session or state.session_id
            ev.busy_reasons.append(
                f"claude {name} ({state.tmux_pane or 'no tmux'}) is {state.state}"
            )

    for pane in panes:
        claudes, others = subtrees[pane.pane_id]
        where = f"{pane.session} {pane.pane_id}"
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


def scan(user: str | None) -> Evaluation:
    return evaluate(
        tmux_panes.list_panes(user),
        process_table.snapshot(),
        session_state.load_all(paths.SESSION_STATE_DIR),
    )


def run(
    user: str,
    grace_seconds: float,
    shutdown: Callable[[], None] | None = None,
    now: Callable[[], float] = time.time,
) -> Decision:
    with state_lock():
        ev = scan(user)
        for key in ev.stale_keys:
            session_state.remove(paths.SESSION_STATE_DIR, key)
        decision = decide(ev.idle, read_idle_since(), now(), grace_seconds)
        if decision.action == "busy":
            cancel_grace()
            print("busy: " + "; ".join(ev.busy_reasons), flush=True)
        elif decision.action == "grace-started":
            write_atomic(paths.IDLE_SINCE, f"{decision.idle_since}\n")
            print(f"idle: grace period of {grace_seconds:.0f}s started", flush=True)
        elif decision.action == "grace":
            print(f"idle: shutdown in {decision.remaining_seconds:.0f}s", flush=True)
        else:
            print("idle: grace period over and still idle; shutting down", flush=True)
            (shutdown or _shutdown_now)()
        return decision


def _shutdown_now() -> None:
    subprocess.run(["/usr/sbin/shutdown", "-h", "now"], check=False)
