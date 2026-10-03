"""Per-Claude-Code-session runtime state (BUSY / READY / IDLE) kept on tmpfs.

One record per running Claude Code process. The record is keyed by the tmux
pane the process runs in (session_id changes on /clear, so it is only an
attribute); outside tmux it falls back to the session_id.
"""

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from cloud_coder_vm.state_lock import write_atomic

BUSY = "BUSY"
READY = "READY"
IDLE = "IDLE"


@dataclass
class SessionState:
    key: str
    session_id: str
    state: str
    tmux_pane: str | None = None
    cloud_coder_session: str | None = None
    claude_pid: int | None = None
    claude_starttime: int | None = None
    cwd: str | None = None
    last_event: str | None = None
    updated_at: float = 0.0


def next_state(current: str | None, payload: dict) -> str | None:
    """Return the state after a hook event, or None when no record should exist.

    Returning ``current`` means the event does not change the state.
    """
    event = payload.get("hook_event_name")
    if event in ("SessionStart", "UserPromptSubmit"):
        return BUSY
    if event == "Stop":
        tasks = payload.get("background_tasks")
        crons = payload.get("session_crons")
        # Missing arrays mean Claude Code could not report its task registry:
        # stay on the safe side and keep the VM up.
        if tasks is None or crons is None or tasks or crons:
            return BUSY
        return READY
    if event == "Notification":
        if payload.get("notification_type") == "idle_prompt" and current == READY:
            return IDLE
        return current
    if event == "SessionEnd":
        return None
    return current


def state_key(tmux_pane: str | None, session_id: str) -> str:
    if tmux_pane:
        return "pane-" + tmux_pane.lstrip("%")
    return "session-" + "".join(c for c in session_id if c.isalnum() or c == "-")


def load(directory: Path, key: str) -> SessionState | None:
    try:
        return SessionState(**json.loads((directory / f"{key}.json").read_text()))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return None


def load_all(directory: Path) -> list[SessionState]:
    states = []
    if not directory.is_dir():
        return states
    for path in sorted(directory.glob("*.json")):
        state = load(directory, path.stem)
        if state is not None:
            states.append(state)
    return states


def save(directory: Path, state: SessionState) -> None:
    state.updated_at = time.time()
    write_atomic(directory / f"{state.key}.json", json.dumps(asdict(state), indent=2) + "\n")


def remove(directory: Path, key: str) -> None:
    (directory / f"{key}.json").unlink(missing_ok=True)
