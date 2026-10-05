"""Command line of the VM agent (cloud-coder-vm.pyz)."""

import argparse
import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from cloud_coder_vm import (
    busy_markers,
    hook,
    idle_check,
    install,
    launch,
    paths,
    session_close,
    session_registry,
    session_screen,
    system_files,
)


def _load_config() -> system_files.VmConfig:
    return system_files.parse_config(paths.CONFIG_PATH.read_text())


def cmd_status(_args) -> int:
    config = _load_config()
    home = Path.home()
    ev = idle_check.scan(config)
    idle_since = idle_check.read_idle_since()
    decision = idle_check.decide(ev.idle, idle_since, time.time(), config.grace_seconds)
    tmux_sessions = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_name}"], capture_output=True, text=True
    ).stdout.split()
    timer = subprocess.run(
        ["systemctl", "is-active", paths.IDLE_TIMER], capture_output=True, text=True
    ).stdout.strip()
    sessions = session_registry.load(paths.registry_path(home))
    states_by_session = {s.cloud_coder_session: s.state for s in ev.live_states}
    out = {
        "idle": ev.idle,
        "busy_reasons": ev.busy_reasons,
        "auto_stop": decision.action if ev.idle else "blocked",
        "idle_since": decision.idle_since if idle_since is not None else None,
        "shutdown_in_seconds": decision.remaining_seconds if idle_since is not None else None,
        "grace_seconds": config.grace_seconds,
        "idle_timer": timer,
        "sessions": [
            {
                **asdict(s),
                "tmux": "running" if s.name in tmux_sessions else "absent",
                "claude_state": states_by_session.get(s.name),
            }
            for s in sorted(sessions.values(), key=lambda s: s.last_connected_at, reverse=True)
        ],
        "claude_processes": [asdict(s) for s in ev.live_states],
    }
    print(json.dumps(out, indent=2))
    return 0


def cmd_launch(args) -> int:
    try:
        result = launch.launch(
            _load_config(),
            Path.home(),
            repo_url=args.repo_url,
            repo=args.repo,
            session_name=args.session,
            new=args.new,
            start_claude=not args.no_claude,
            prompt=base64.b64decode(args.prompt_b64).decode() if args.prompt_b64 else None,
        )
    except launch.LaunchError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(result))
    return 0


def cmd_close_session(args) -> int:
    try:
        result = session_close.close(_load_config(), Path.home(), args.session)
    except session_close.CloseError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(result))
    return 0


def cmd_read_session(args) -> int:
    try:
        result = session_screen.read(Path.home(), args.session, args.lines)
    except session_screen.ScreenError as e:
        print(json.dumps({"error": str(e)}))
        return 1
    print(json.dumps(result))
    return 0


def cmd_install_system(args) -> int:
    if os.geteuid() != 0:
        print("install-system must run as root", file=sys.stderr)
        return 1
    config = system_files.parse_config(args.config)
    with busy_markers.busy_marker("install", required=False):
        install.install_system(Path(sys.argv[0]).resolve(), config)
    return 0


def cmd_install_user(_args) -> int:
    with busy_markers.busy_marker("install", required=False):
        install.install_user(_load_config(), Path.home())
    return 0


def cmd_idle_check(_args) -> int:
    config = _load_config()
    idle_check.run(config)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cloud-coder-vm")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("hook", help="Claude Code hook (payload on stdin)").set_defaults(
        func=lambda _a: hook.main()
    )
    sub.add_parser("idle-check", help="evaluate idleness; shut down after grace").set_defaults(
        func=cmd_idle_check
    )
    sub.add_parser("status", help="print VM / session state as JSON").set_defaults(func=cmd_status)

    p = sub.add_parser("launch", help="ensure repo, tmux session and Claude Code")
    p.add_argument("--repo-url")
    p.add_argument("--repo")
    p.add_argument("--session")
    p.add_argument("--new", action="store_true")
    p.add_argument("--no-claude", action="store_true", help="prepare repo and tmux only")
    p.add_argument("--prompt-b64", help="first prompt for Claude Code, base64 encoded (UTF-8)")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("read-session", help="print a session's Claude Code pane as JSON")
    p.add_argument("--session", required=True)
    p.add_argument("--lines", type=int, default=200, help="lines to return, scrollback included")
    p.set_defaults(func=cmd_read_session)

    p = sub.add_parser(
        "close-session", help="end a session and remove its worktree when nothing is unsaved"
    )
    p.add_argument("--session", required=True)
    p.set_defaults(func=cmd_close_session)

    p = sub.add_parser("install-system", help="(root) install units and files")
    p.add_argument("--config", required=True, help="VM config as JSON")
    p.set_defaults(func=cmd_install_system)

    sub.add_parser("install-user", help="(user) Claude Code and hooks").set_defaults(
        func=cmd_install_user
    )

    args = parser.parse_args(argv)
    return args.func(args)
