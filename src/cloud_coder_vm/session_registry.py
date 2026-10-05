"""Persistent map: cloud-coder session <-> repo / worktree <-> Claude Code session ID.

Stored under HOME (Persistent Disk) so it survives VM stops. Only the mapping
is kept; conversation data stays in Claude Code's own transcripts.
The cloud-coder session name doubles as the tmux session name.
"""

import json
import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from cloud_coder_vm.state_lock import write_atomic

NAME_PREFIX = "cc-"


@dataclass
class LogicalSession:
    name: str
    repo: str
    repo_url: str | None
    workdir: str
    claude_session_id: str
    created_at: float
    last_connected_at: float


def repo_name_from_url(url: str) -> str:
    """git@github.com:o/r.git, https://github.com/o/r(.git), /path/r -> r"""
    tail = re.split(r"[/:]", url.rstrip("/"))[-1]
    if tail.endswith(".git"):
        tail = tail[:-4]
    if not tail or tail in (".", ".."):
        raise ValueError(f"cannot derive a repository name from {url!r}")
    return tail


def canonical_url(url: str) -> str:
    """Same repository, whatever the transport: git@h:o/r.git, ssh://git@h/o/r, https://h/o/r"""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "://" in url:
        rest = url.split("://", 1)[1]
    elif re.match(r"^[^/]+@[^/:]+:", url) or re.match(r"^[^/:]+:[^/]", url):
        rest = url.replace(":", "/", 1)
    else:
        return url  # local path
    rest = rest.split("@", 1)[-1]
    host, _, path = rest.partition("/")
    return f"{host.split(':')[0].lower()}/{path}"


def session_name(repo: str, index: int) -> str:
    # tmux forbids ':' and '.' in session names.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", repo)
    return f"{NAME_PREFIX}{safe}-{index}"


def next_index(
    sessions: dict[str, LogicalSession],
    repo: str,
    left_behind: Callable[[int], bool] = lambda index: False,
) -> int:
    """The lowest index whose name is neither registered nor ``left_behind`` by an
    earlier session (see `launch.left_behind`)."""
    index = 1
    while session_name(repo, index) in sessions or left_behind(index):
        index += 1
    return index


def latest(sessions: dict[str, LogicalSession], repo: str | None = None) -> LogicalSession | None:
    candidates = [s for s in sessions.values() if repo is None or s.repo == repo]
    return max(candidates, key=lambda s: s.last_connected_at, default=None)


def load(path: Path) -> dict[str, LogicalSession]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    return {name: LogicalSession(**entry) for name, entry in raw.get("sessions", {}).items()}


def save(path: Path, sessions: dict[str, LogicalSession]) -> None:
    raw = {"sessions": {name: asdict(s) for name, s in sorted(sessions.items())}}
    write_atomic(path, json.dumps(raw, indent=2) + "\n", mode=0o600)
