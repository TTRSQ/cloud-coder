"""Per-Claude-Code-session runtime state (BUSY / READY / IDLE) kept on tmpfs.

One record per running Claude Code process. The record is keyed by the tmux
pane the process runs in (session_id changes on /clear, so it is only an
attribute); outside tmux it falls back to the session_id. The key does not tell
tmux servers apart: a Claude Code in pane %N of the default server (see tmux_panes)
shares the record of the one in %N of cloud-coder's. That only errs towards busy.
"""

import hashlib
import json
import re
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

# How many of the latest submitted prompts a record keeps, to confirm that a typed prompt
# was received. Prompts Claude Code makes itself (a background task's notification) fire
# UserPromptSubmit too, so a count alone could confirm a prompt that was lost.
RECENT_PROMPTS = 8
_PASTE_TAG = re.compile(r"</?pasted_content\b[^>]*>")


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
    open_dialogs: list[dict] = field(default_factory=list)  # dialog()s, oldest first
    prompts_submitted: int = 0  # UserPromptSubmit events
    recent_prompt_digests: list[str] = field(default_factory=list)  # prompt_digest()s, newest last

    def __post_init__(self):
        # an agent from before dialog() kept only the ids
        self.open_dialogs = [
            d if isinstance(d, dict) else {"id": d, "kind": "unknown"} for d in self.open_dialogs
        ]


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


def prompt_digest(prompt: str) -> str:
    """Digest of a prompt without whitespace and without the tags Claude Code wraps a long
    paste in: Claude Code turns tabs into spaces, drops trailing spaces and adds those tags."""
    typed = "".join(_PASTE_TAG.sub("", prompt).split())
    return hashlib.sha256(typed.encode()).hexdigest()[:16]


def record_prompt(state: SessionState, prompt: str) -> None:
    state.prompts_submitted += 1
    digests = [*state.recent_prompt_digests, prompt_digest(prompt)]
    state.recent_prompt_digests = digests[-RECENT_PROMPTS:]


def received_since(before: SessionState, after: SessionState, prompt: str) -> bool:
    """Whether a prompt submitted after ``before`` is ``prompt``, up to what Claude Code
    changes (see prompt_digest). The whole text must match: a short prompt may well occur
    inside another one, such as a background task's notification."""
    new = after.prompts_submitted - before.prompts_submitted
    if new <= 0:
        return False
    return prompt_digest(prompt) in after.recent_prompt_digests[-new:]


def dialog_id(payload: dict) -> str:
    """What a dialog asks about, so that its closing event finds it: the tool call (the same
    tool_input reaches PermissionRequest and PostToolUse), or the MCP server eliciting."""
    if payload.get("hook_event_name") in ("Elicitation", "ElicitationResult"):
        subject = ["elicitation", payload.get("mcp_server_name")]
    else:
        subject = [payload.get("tool_name"), payload.get("tool_input")]
    encoded = json.dumps(subject, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()[:16]


# Longest text a dialog keeps from what it asks, so the state on tmpfs stays small.
DIALOG_TEXT_LIMIT = 500
# Inputs of common tools that say what a permission prompt is about, best first.
_SUBJECT_KEYS = ("command", "file_path", "notebook_path", "path", "url", "pattern", "query")


def _clip(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= DIALOG_TEXT_LIMIT else value[:DIALOG_TEXT_LIMIT] + "…"


def dialog(payload: dict) -> dict:
    """A dialog Claude Code shows, from the hook event that shows it: its ``id`` and what
    it waits for, short enough to keep. Never the whole tool input (a Write's content)."""
    found: dict = {"id": dialog_id(payload)}
    if payload.get("hook_event_name") == "Elicitation":
        return {**found, "kind": "mcp_input", "server": payload.get("mcp_server_name")}
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool == "AskUserQuestion":
        questions = [
            {
                "question": _clip(q.get("question")),
                "options": [
                    _clip(o.get("label")) for o in q.get("options") or [] if isinstance(o, dict)
                ],
                "multi_select": bool(q.get("multiSelect")),
            }
            for q in tool_input.get("questions") or []
            if isinstance(q, dict)
        ]
        return {**found, "kind": "question", "questions": questions}
    if tool == "ExitPlanMode":
        return {**found, "kind": "plan_approval", "plan": _clip(tool_input.get("plan"))}
    subject = next(
        (tool_input[k] for k in _SUBJECT_KEYS if isinstance(tool_input.get(k), str)), None
    )
    return {**found, "kind": "permission", "tool": tool, "subject": _clip(subject)}


def waiting_for(state: "SessionState") -> list[dict]:
    """What the dialogs Claude Code shows wait for, without their ids."""
    return [{k: v for k, v in d.items() if k != "id"} for d in state.open_dialogs]


def _is_shell(task: object) -> bool:
    return isinstance(task, dict) and task.get("type") == "shell"


def next_dialogs(dialogs: list[dict], payload: dict) -> list[dict]:
    """The dialogs still shown after a hook event. A dialog denied with Esc or "No" fires
    no event: it counts as shown until a turn ends with no background task left that may
    ask for a permission (any but a shell) or Claude Code restarts, which errs towards
    not typing."""
    event = payload.get("hook_event_name")
    if event in DIALOG_SHOWN:
        return [*dialogs, dialog(payload)]
    if event in DIALOG_CLOSED:
        closed = dialog_id(payload)
        index = next((i for i, d in enumerate(dialogs) if d.get("id") == closed), None)
        if index is None:
            return dialogs
        return dialogs[:index] + dialogs[index + 1 :]
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
