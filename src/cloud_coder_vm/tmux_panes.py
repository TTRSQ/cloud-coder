"""Read the panes of the cloud-coder user's tmux server."""

import os
import subprocess
from dataclasses import dataclass

PANE_FORMAT = "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}"


@dataclass(frozen=True)
class Pane:
    session: str
    pane_id: str
    pane_pid: int
    current_command: str


def parse_list_panes(output: str) -> list[Pane]:
    panes = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        panes.append(Pane(parts[0], parts[1], int(parts[2]), parts[3]))
    return panes


def tmux_command(user: str | None) -> list[str]:
    """tmux invocation that talks to ``user``'s default server, also when run as root."""
    if user and os.geteuid() == 0:
        return ["runuser", "-u", user, "--", "tmux"]
    return ["tmux"]


def no_server(stderr: str) -> bool:
    return "no server running" in stderr or (
        "error connecting to" in stderr and "No such file or directory" in stderr
    )


def list_panes(user: str | None = None, timeout: float | None = None) -> list[Pane] | None:
    """Panes of all sessions; [] when no server runs, None when tmux could not be queried."""
    try:
        result = subprocess.run(
            [*tmux_command(user), "list-panes", "-a", "-F", PANE_FORMAT],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return [] if no_server(result.stderr) else None
    return parse_list_panes(result.stdout)
