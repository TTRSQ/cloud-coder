"""Per-Claude-Code-session runtime state (BUSY / READY / IDLE) kept on tmpfs.

One record per running Claude Code process. The record is keyed by the tmux
pane the process runs in (session_id changes on /clear, so it is only an
attribute); outside tmux it falls back to the session_id. The key does not tell
tmux servers apart: a Claude Code in pane %N of the default server (see tmux_panes)
shares the record of the one in %N of cloud-coder's. That only errs towards busy.
"""

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cloud_coder_vm.state_lock import write_atomic

BUSY = "BUSY"
READY = "READY"
IDLE = "IDLE"

# Hook events that show or close a dialog: a permission prompt (AskUserQuestion included)
# or an MCP elicitation. While one is shown, Enter would answer it, so no prompt is typed.
# They leave BUSY / READY / IDLE alone.
DIALOG_SHOWN = ("PermissionRequest", "Elicitation")
DIALOG_CLOSED = ("PostToolUse", "PostToolUseFailure", "ElicitationResult")


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
    open_dialogs: list[str] = field(default_factory=list)
    prompts_submitted: int = 0  # UserPromptSubmit events: a typed prompt was received


def next_state(current: str | None, payload: dict, last_event: str | None = None) -> str | None:
    """Return the state after a hook event, or None when no record should exist.

    Returning ``current`` means the event does not change the state.
    """
    event = payload.get("hook_event_name")
    if event == "SessionStart":
        # A brand-new process with a new conversation has no background tasks and no
        # crons yet, and is waiting for input: idle until a prompt is submitted.
        # resume (restores crons), clear, compact and fork stay on the safe side.
        return IDLE if payload.get("source") == "startup" else BUSY
    if event == "UserPromptSubmit":
        return BUSY
    if event == "Stop":
        tasks = payload.get("background_tasks")
        crons = payload.get("session_crons")
        # Missing arrays mean Claude Code could not report its task registry:
        # stay on the safe side and keep the VM up.
        if tasks is None or crons is None or tasks or crons:
            return BUSY
        return READY
    if event == "StopFailure":
        # The turn ended on an API error. No task registry is reported, so do not go
        # straight to IDLE: idle_prompt (which waits for background agents) decides.
        return READY
    if event == "Notification":
        if payload.get("notification_type") == "idle_prompt":
            if current == READY:
                return IDLE
            # BUSY only because a session (re)started (resume, /clear, ...) and no turn
            # has run since: Claude Code is waiting for input.
            if current == BUSY and last_event == "SessionStart":
                return IDLE
        return current
    if event == "SessionEnd":
        return None
    return current


def dialog_id(payload: dict) -> str:
    """What a dialog asks about, so that its closing event finds it: the tool call (the same
    tool_input reaches PermissionRequest and PostToolUse), or the MCP server eliciting."""
    if payload.get("hook_event_name") in ("Elicitation", "ElicitationResult"):
        subject = ["elicitation", payload.get("mcp_server_name")]
    else:
        subject = [payload.get("tool_name"), payload.get("tool_input")]
    encoded = json.dumps(subject, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


def _is_shell(task: object) -> bool:
    return isinstance(task, dict) and task.get("type") == "shell"


def next_dialogs(dialogs: list[str], payload: dict) -> list[str]:
    """The dialogs still shown after a hook event. A dialog denied with Esc or "No" fires
    no event: it counts as shown until a turn ends with no background task left that may
    ask for a permission (any but a shell) or Claude Code restarts, which errs towards
    not typing."""
    event = payload.get("hook_event_name")
    if event in DIALOG_SHOWN:
        return [*dialogs, dialog_id(payload)]
    if event in DIALOG_CLOSED:
        closed = dialog_id(payload)
        if closed in dialogs:
            remaining = list(dialogs)
            remaining.remove(closed)
            return remaining
        return dialogs
    tasks = payload.get("background_tasks")
    if event == "Stop" and isinstance(tasks, list) and all(_is_shell(t) for t in tasks):
        return []
    if event in ("SessionStart", "SessionEnd"):
        return []
    return dialogs


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
