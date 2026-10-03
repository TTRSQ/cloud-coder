"""Claude Code hook entry point: records BUSY / READY / IDLE for the calling session.

Registered for SessionStart, UserPromptSubmit, Stop, Notification(idle_prompt)
and SessionEnd. Reads the hook payload from stdin and never fails the hook.
"""

import json
import os
import sys
import time
import traceback
from collections.abc import Mapping
from pathlib import Path

from cloud_coder_vm import idle_check, paths, process_table, session_registry, session_state
from cloud_coder_vm.session_state import BUSY, SessionState
from cloud_coder_vm.state_lock import state_lock


def claude_process_of(pid: int) -> process_table.Process | None:
    """Walk up from the hook process to the Claude Code process that spawned it."""
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        proc = process_table.read_process(pid)
        if proc is None:
            return None
        if process_table.is_claude(proc):
            return proc
        pid = proc.ppid
    return None


def apply_event(
    payload: dict,
    env: Mapping[str, str],
    state_dir: Path,
    registry_file: Path,
    claude_proc: process_table.Process | None,
) -> str | None:
    """Update runtime state (and the registry on SessionStart). Returns the new state."""
    session_id = str(payload.get("session_id", ""))
    pane = env.get("TMUX_PANE") or None
    logical = env.get("CLOUD_CODER_SESSION") or None
    key = session_state.state_key(pane, session_id)
    current = session_state.load(state_dir, key)
    event = payload.get("hook_event_name")

    if event == "SessionEnd" and current is not None and current.session_id != session_id:
        return current.state  # end of a session this pane already replaced (/clear, /resume)

    new = session_state.next_state(current.state if current else None, payload)
    if new is None:
        session_state.remove(state_dir, key)
    else:
        record = current or SessionState(key=key, session_id=session_id, state=new)
        record.session_id = session_id
        record.state = new
        record.tmux_pane = pane
        record.cloud_coder_session = logical
        record.cwd = payload.get("cwd") or record.cwd
        record.last_event = event
        if claude_proc is not None:
            record.claude_pid = claude_proc.pid
            record.claude_starttime = claude_proc.starttime
        session_state.save(state_dir, record)

    if event == "SessionStart" and logical and session_id:
        sessions = session_registry.load(registry_file)
        entry = sessions.get(logical)
        if entry is not None and entry.claude_session_id != session_id:
            entry.claude_session_id = session_id
            session_registry.save(registry_file, sessions)
    return new


def main(stdin=sys.stdin) -> int:
    if not paths.RUNTIME_DIR.is_dir():
        return 0
    try:
        payload = json.load(stdin)
        with state_lock():
            new = apply_event(
                payload,
                os.environ,
                paths.SESSION_STATE_DIR,
                paths.registry_path(Path.home()),
                claude_process_of(os.getppid()),
            )
            if new == BUSY:
                idle_check.cancel_grace()
    except Exception:
        try:
            with open(paths.HOOK_LOG, "a") as log:
                log.write(f"{time.strftime('%FT%T')} {traceback.format_exc()}\n")
        except OSError:
            pass
    return 0
