"""cloud-coder command line."""

import argparse
import json
import logging
import sys
from pathlib import Path

from cloud_coder import config as config_mod
from cloud_coder import connect, gce, ssh
from cloud_coder.config import Config


def _common_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--config", help="config file (default ~/.config/cloud-coder/config.yaml)")
    p.add_argument("--project")
    p.add_argument("--zone")
    p.add_argument("--instance")
    p.add_argument("--machine-type")
    p.add_argument("--disk-size-gb", type=int)
    p.add_argument("--disk-type")
    p.add_argument(
        "--iap",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="SSH through Identity-Aware Proxy",
    )
    return p


def resolve_config(args) -> Config:
    cfg = config_mod.load(config_mod.config_path(args.config))
    cfg = config_mod.with_overrides(
        cfg,
        project=args.project,
        zone=args.zone,
        instance=args.instance,
        machine_type=args.machine_type,
        disk_size_gb=args.disk_size_gb,
        disk_type=args.disk_type,
        iap=args.iap,
    )
    if not cfg.project:
        raise config_mod.ConfigError(
            f"no GCP project: set gcp.project in {config_mod.config_path(args.config)} "
            "or pass --project"
        )
    return cfg


def read_prompt(args) -> str | None:
    if args.prompt is not None:
        return args.prompt
    if args.prompt_file is None:
        return None
    if args.prompt_file == "-":
        return sys.stdin.read()
    return Path(args.prompt_file).expanduser().read_text()


def build_parser() -> argparse.ArgumentParser:
    common = _common_parser()
    parser = argparse.ArgumentParser(
        prog="cloud-coder",
        description="Claude Code worker on a GCE VM, driven over tmux, stopped when idle.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("up", parents=[common], help="create/start the VM and install the agent")

    p = sub.add_parser(
        "connect", parents=[common], help="start everything needed and attach to the session's tmux"
    )
    p.add_argument(
        "repo",
        nargs="?",
        help="git URL to clone, or a repo name already in the workspace; without a prompt "
        "the default is the most recently used session",
    )
    p.add_argument(
        "--new",
        action="store_true",
        help="start another session for the repo in a new git worktree (the default with a prompt)",
    )
    p.add_argument(
        "--session",
        help="connect to this session (name from `status`); with a prompt, the way to "
        "continue its conversation",
    )
    p.add_argument(
        "--no-attach",
        "--detach",
        dest="no_attach",
        action="store_true",
        help="prepare (and send the prompt) but do not attach",
    )
    prompt = p.add_mutually_exclusive_group()
    prompt.add_argument(
        "-p",
        "--prompt",
        help="a task for Claude Code: starts a new session and conversation for REPO, or "
        "continues the one named by --session (sent only when Claude Code is READY or IDLE)",
    )
    prompt.add_argument("--prompt-file", help="read the prompt from a file ('-' for stdin)")
    p.add_argument("--no-claude", action="store_true", help="do not start Claude Code")

    sub.add_parser(
        "status", parents=[common], help="VM, sessions and auto-stop state"
    ).add_argument("--json", action="store_true")
    p = sub.add_parser(
        "close",
        parents=[common],
        help="end a session (and its Claude Code), remove its worktree and forget it; "
        "regenerable caches (target/, .venv/, node_modules/, ...) go with it, anything "
        "else unsaved (uncommitted, unpushed, other ignored files) refuses the close",
    )
    p.add_argument("session", help="session name (see `status`)")
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="only report what close would do: blockers, caches and the space it frees",
    )
    p.add_argument(
        "--discard-ignored",
        action="store_true",
        help="also delete ignored files that are not regenerable caches (.env, out/, ...); "
        "uncommitted and unpushed work still refuses the close",
    )
    sub.add_parser("stop", parents=[common], help="stop the VM (disk is kept)")
    sub.add_parser(
        "mcp", parents=[common], help="serve the VM and its sessions as MCP tools over stdio"
    )
    p = sub.add_parser(
        "api",
        parents=[common],
        help="serve the MCP server over HTTP at /mcp (OAuth settings from the environment)",
    )
    p.add_argument("--host", default="127.0.0.1", help="address to bind (default 127.0.0.1)")
    p.add_argument("--port", type=int, default=8787, help="port to bind (default 8787)")
    return parser


def log_to_stderr() -> None:
    logger = logging.getLogger("cloud_coder")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("cloud-coder: %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    log_to_stderr()
    try:
        cfg = resolve_config(args)
        print(
            f"cloud-coder: target project={cfg.project} zone={cfg.zone} instance={cfg.instance}",
            file=sys.stderr,
        )
        requested = args.machine_type is not None
        if args.command == "up":
            connect.up(cfg, requested)
            return 0
        if args.command == "connect":
            launched = connect.connect(
                cfg,
                args.repo,
                prompt=read_prompt(args),
                new=args.new,
                session=args.session,
                no_claude=args.no_claude,
                machine_type_requested=requested,
            )
            print(json.dumps(launched))
            if args.no_attach:
                return 0
            return ssh.attach_tmux(cfg, launched["session"])
        if args.command == "close":
            connect.up(cfg, requested)
            closed = connect.close_session(
                cfg, args.session, dry_run=args.dry_run, discard_ignored=args.discard_ignored
            )
            print(json.dumps(closed, indent=2 if args.dry_run else None))
            return 0
        if args.command == "status":
            st = connect.status(cfg)
            print(json.dumps(st, indent=2) if args.json else connect.format_status(st))
            return 0
        if args.command == "stop":
            print(f"VM {cfg.instance}: {gce.stop(cfg)}")
            return 0
        if args.command == "mcp":
            # imported here so that the other commands do not load the MCP SDK
            from cloud_coder import mcp_server

            mcp_server.build_server(cfg).run("stdio")
            return 0
        if args.command == "api":
            from cloud_coder import http_api

            http_api.serve(cfg, args.host, args.port)
            return 0
    except (config_mod.ConfigError, gce.GcloudError, RuntimeError, TimeoutError, OSError) as e:
        print(f"cloud-coder: error: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
