"""The cloud-coder user's tmux servers: cloud-coder's own and the default one.

cloud-coder runs its sessions on a tmux server of its own (``tmux -L cloud-coder``) and
starts Claude Code without ``$TMUX``, so a bare ``tmux`` run by work inside a session
(``tmux kill-server`` in a test, say) reaches the default server, never cloud-coder's.
"""

import os
import subprocess
from dataclasses import dataclass

PANE_FORMAT = "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{pane_current_command}"


# tmux socket name (-L) of the server cloud-coder's sessions run on
SOCKET = "cloud-coder"
# the server a bare `tmux` uses; it held cloud-coder's sessions before SOCKET existed
DEFAULT_SOCKET = "default"


@dataclass(frozen=True)
class Pane:
    session: str
    pane_id: str
    pane_pid: int
    current_command: str
    socket: str = SOCKET


def parse_list_panes(output: str, socket: str = SOCKET) -> list[Pane]:
    panes = []
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[2].isdigit():
            continue
        panes.append(Pane(parts[0], parts[1], int(parts[2]), parts[3], socket))
    return panes


def tmux_command(user: str | None = None, socket: str = SOCKET) -> list[str]:
    """tmux invocation that talks to ``user``'s server ``socket``, also when run as root."""
    tmux = ["tmux", "-L", socket]
    if user and os.geteuid() == 0:
        return ["runuser", "-u", user, "--", *tmux]
    return tmux


def no_server(stderr: str) -> bool:
    return "no server running" in stderr or (
        "error connecting to" in stderr and "No such file or directory" in stderr
    )


def list_panes(
    user: str | None = None, timeout: float | None = None, socket: str = SOCKET
) -> list[Pane] | None:
    """Panes of all sessions of one server; [] when it does not run, None when it could
    not be queried."""
    try:
        result = subprocess.run(
            [*tmux_command(user, socket), "list-panes", "-a", "-F", PANE_FORMAT],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return [] if no_server(result.stderr) else None
    return parse_list_panes(result.stdout, socket)


def list_all_panes(user: str | None = None, timeout: float | None = None) -> list[Pane] | None:
    """Panes of cloud-coder's server and of the default one, where work started from a
    session with a bare `tmux` (and sessions of an older cloud-coder) end up; None when
    either could not be queried."""
    own = list_panes(user, timeout, SOCKET)
    default = list_panes(user, timeout, DEFAULT_SOCKET)
    if own is None or default is None:
        return None
    return own + default
