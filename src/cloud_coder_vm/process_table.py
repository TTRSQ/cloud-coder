"""Snapshot of /proc used to inspect what runs under each tmux pane."""

import os
from dataclasses import dataclass
from pathlib import Path

SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh"})
# Run a shell with other credentials and wait for it (launch uses `sg docker`).
WRAPPERS = frozenset({"sg", "newgrp"})


@dataclass(frozen=True)
class Process:
    pid: int
    ppid: int
    argv0: str
    exe: str = ""
    starttime: int = 0
    tty: int = 0  # controlling terminal device number, 0 if none


def is_shell(proc: Process) -> bool:
    return os.path.basename(proc.argv0).lstrip("-") in SHELLS


def is_claude(proc: Process) -> bool:
    """Claude Code as installed by the native installer or via npm."""
    if os.path.basename(proc.argv0) == "claude":
        return True
    return "/claude/versions/" in proc.exe or "@anthropic-ai/claude-code" in proc.argv0


def is_wrapper(proc: Process) -> bool:
    return os.path.basename(proc.argv0) in WRAPPERS


def pane_subtree(
    pane_pid: int, procs: dict[int, Process], children: dict[int, list[int]]
) -> tuple[list[Process], list[Process]]:
    """(Claude processes, other processes) under a pane. Does not descend into Claude
    and skips credential wrappers, so the first "other" is the pane's shell."""
    claudes, others = [], []
    stack = [pane_pid]
    while stack:
        proc = procs.get(stack.pop())
        if proc is None:
            continue
        if is_claude(proc):
            claudes.append(proc)
            continue
        if not (is_wrapper(proc) and proc.pid == pane_pid):
            others.append(proc)
        stack.extend(children.get(proc.pid, []))
    return claudes, others


def children_map(procs: dict[int, Process]) -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for proc in procs.values():
        children.setdefault(proc.ppid, []).append(proc.pid)
    for pids in children.values():
        pids.sort()
    return children


def read_process(pid: int, proc_root: Path = Path("/proc")) -> Process | None:
    base = proc_root / str(pid)
    try:
        stat = (base / "stat").read_text()
        cmdline = (base / "cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    # comm (field 2) may contain spaces and parentheses; fields resume after the last ')'.
    fields = stat[stat.rindex(")") + 2 :].split()
    ppid = int(fields[1])
    tty = int(fields[4])
    starttime = int(fields[19])
    argv = [a.decode(errors="replace") for a in cmdline if a]
    if not argv:  # kernel thread
        argv0 = ""
    elif os.path.basename(argv[0]) in ("node", "nodejs") and len(argv) > 1:
        argv0 = argv[1] if "claude" in argv[1] else argv[0]
    else:
        argv0 = argv[0]
    try:
        exe = os.readlink(base / "exe")
    except OSError:
        exe = ""
    return Process(pid=pid, ppid=ppid, argv0=argv0, exe=exe, starttime=starttime, tty=tty)


def snapshot(proc_root: Path = Path("/proc")) -> dict[int, Process]:
    procs = {}
    for entry in os.listdir(proc_root):
        if entry.isdigit():
            proc = read_process(int(entry), proc_root)
            if proc is not None:
                procs[proc.pid] = proc
    return procs


def find_claude_ancestor(pid: int, procs: dict[int, Process]) -> Process | None:
    seen = set()
    while pid in procs and pid not in seen and pid > 1:
        seen.add(pid)
        proc = procs[pid]
        if is_claude(proc):
            return proc
        pid = proc.ppid
    return None
