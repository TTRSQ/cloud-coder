"""Pre-accept Claude Code's workspace trust dialog for directories cloud-coder manages.

Claude Code stores accepted trust as ``projects["<repo root>"].hasTrustDialogAccepted``
in ``~/.claude.json`` (https://code.claude.com/docs/en/permissions, "What runs before
you trust a folder"). Inside a git worktree it keys on the main checkout's root.
"""

import json
import os
from pathlib import Path

from cloud_coder_vm.state_lock import write_atomic


def with_trusted(config: dict, directories: list[str]) -> dict | None:
    """Return a copy of ``config`` with the directories trusted, or None if already trusted."""
    projects = config.get("projects")
    if projects is not None and not isinstance(projects, dict):
        raise ValueError("~/.claude.json has a non-object 'projects' entry")
    projects = dict(projects or {})
    changed = False
    for directory in directories:
        entry = projects.get(directory)
        entry = dict(entry) if isinstance(entry, dict) else {}
        if entry.get("hasTrustDialogAccepted") is True:
            continue
        entry["hasTrustDialogAccepted"] = True
        projects[directory] = entry
        changed = True
    if not changed:
        return None
    return {**config, "projects": projects}


def is_under(directory: Path, root: Path) -> bool:
    directory, root = directory.resolve(), root.resolve()
    return directory != root and directory.is_relative_to(root)


def trust(config_path: Path, directories: list[Path], roots: list[Path]) -> bool:
    """Trust ``directories``; each must be strictly inside one of ``roots`` (the directories
    cloud-coder clones into). Returns True if the file was written."""
    for directory in directories:
        is_root = any(directory.resolve() == root.resolve() for root in roots)
        if is_root or not any(is_under(directory, root) for root in roots):
            names = ", ".join(str(r) for r in roots)
            raise ValueError(f"refusing to trust {directory}: not inside {names}")
    # Update the real file if ~/.claude.json is a symlink, instead of replacing the link.
    config_path = config_path.resolve()
    try:
        text = config_path.read_text()
        mode = os.stat(config_path).st_mode & 0o777
    except FileNotFoundError:
        text, mode = "{}", 0o600
    config = json.loads(text) if text.strip() else {}
    # Read-modify-write is kept to milliseconds; Claude Code rewrites this file too.
    updated = with_trusted(config, [str(d.resolve()) for d in directories])
    if updated is None:
        return False
    write_atomic(config_path, json.dumps(updated, indent=2) + "\n", mode=mode)
    return True
