"""Read what a cloud-coder session shows: the text of the tmux pane Claude Code runs in."""

import subprocess

from cloud_coder_vm import paths, process_table, session_state
from cloud_coder_vm.tmux_panes import PANE_FORMAT, Pane, parse_list_panes, tmux_command


class ScreenError(Exception):
    pass


def claude_pane(panes: list[Pane]) -> tuple[Pane, int | None]:
    """The pane Claude Code runs in, with its pid; the session's first pane when none does."""
    procs = process_table.snapshot()
    children = process_table.children_map(procs)
    for pane in panes:
        claudes, _ = process_table.pane_subtree(pane.pane_pid, procs, children)
        if claudes:
            return pane, claudes[0].pid
    return panes[0], None


def visible_text(captured: str) -> str:
    """Pane text without the blank rows below the cursor."""
    return "\n".join(line.rstrip() for line in captured.rstrip("\n").split("\n")).rstrip("\n")


def read(session_name: str) -> dict:
    """The visible text of the session's Claude Code pane. Claude Code draws in the
    alternate screen, which keeps no scrollback; its responses are in its transcript."""
    listed = subprocess.run(
        [*tmux_command(), "list-panes", "-s", "-t", f"={session_name}", "-F", PANE_FORMAT],
        capture_output=True,
        text=True,
    )
    panes = parse_list_panes(listed.stdout) if listed.returncode == 0 else []
    if not panes:
        return {
            "session": session_name,
            "tmux": "absent",
            "pane": None,
            "claude": "not running",
            "claude_state": None,
            "output": "",
        }
    pane, claude_pid = claude_pane(panes)
    captured = subprocess.run(
        [*tmux_command(), "capture-pane", "-p", "-J", "-t", pane.pane_id],
        capture_output=True,
        text=True,
    )
    if captured.returncode != 0:
        raise ScreenError(f"tmux capture-pane failed: {captured.stderr.strip()}")
    state = None
    if claude_pid is not None:
        state = next(
            (
                s.state
                for s in session_state.load_all(paths.SESSION_STATE_DIR)
                if s.claude_pid == claude_pid
                or (s.claude_pid is None and s.tmux_pane == pane.pane_id)
            ),
            None,
        )
    return {
        "session": session_name,
        "tmux": "running",
        "pane": pane.pane_id,
        "claude": "running" if claude_pid is not None else "not running",
        "claude_state": state,
        "output": visible_text(captured.stdout),
    }
