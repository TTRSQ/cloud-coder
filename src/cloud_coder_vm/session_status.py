"""How a cloud-coder session's Claude Code is doing, from structured sources only: tmux
(does the session exist), the process table (does Claude Code run) and the hook state
(BUSY / READY / IDLE and the dialogs it shows). Nothing is read off the screen."""

import subprocess

from cloud_coder_vm import paths, process_table, session_state
from cloud_coder_vm.tmux_panes import PANE_FORMAT, Pane, parse_list_panes, tmux_command


def claude_pane(panes: list[Pane]) -> tuple[Pane, int | None]:
    """The pane Claude Code runs in, with its pid; the session's first pane when none does."""
    procs = process_table.snapshot()
    children = process_table.children_map(procs)
    for pane in panes:
        claudes, _ = process_table.pane_subtree(pane.pane_pid, procs, children)
        if claudes:
            return pane, claudes[0].pid
    return panes[0], None


def read(session_name: str) -> dict:
    """``tmux`` (running / absent), ``claude`` (running / not running), ``claude_state``
    (None while Claude Code runs but has not reported, as on its login or trust screen)
    and ``waiting_for`` (what the dialogs it shows ask, see session_state.dialog)."""
    listed = subprocess.run(
        [*tmux_command(), "list-panes", "-s", "-t", f"={session_name}", "-F", PANE_FORMAT],
        capture_output=True,
        text=True,
    )
    panes = parse_list_panes(listed.stdout) if listed.returncode == 0 else []
    if not panes:
        return {"tmux": "absent", "claude": "not running", "claude_state": None, "waiting_for": []}
    pane, claude_pid = claude_pane(panes)
    state = None
    if claude_pid is not None:
        state = next(
            (
                s
                for s in session_state.load_all(paths.SESSION_STATE_DIR)
                if s.claude_pid == claude_pid
                or (s.claude_pid is None and s.tmux_pane == pane.pane_id)
            ),
            None,
        )
    return {
        "tmux": "running",
        "claude": "running" if claude_pid is not None else "not running",
        "claude_state": state.state if state else None,
        "waiting_for": session_state.waiting_for(state) if state else [],
    }
