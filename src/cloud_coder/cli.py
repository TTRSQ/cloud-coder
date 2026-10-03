"""cloud-coder command line."""

import argparse
import json
import sys

from cloud_coder import config as config_mod
from cloud_coder import connect, gce
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
    if cfg.project is None:
        cfg = config_mod.with_overrides(cfg, project=gce.default_project())
    if cfg.project is None:
        raise config_mod.ConfigError("no GCP project: set gcp.project or pass --project")
    return cfg


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
        help="git URL to clone, or a repo name already in the workspace "
        "(default: the most recently used session)",
    )
    p.add_argument(
        "--new",
        action="store_true",
        help="start another session for the repo in a new git worktree",
    )
    p.add_argument("--session", help="connect to this session name (see `status`)")
    p.add_argument("--no-attach", action="store_true", help="prepare but do not attach")
    p.add_argument("--no-claude", action="store_true", help="do not start Claude Code")

    sub.add_parser(
        "status", parents=[common], help="VM, sessions and auto-stop state"
    ).add_argument("--json", action="store_true")
    sub.add_parser("stop", parents=[common], help="stop the VM (disk is kept)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = resolve_config(args)
        requested = args.machine_type is not None
        if args.command == "up":
            connect.up(cfg, requested)
            return 0
        if args.command == "connect":
            return connect.connect(
                cfg,
                args.repo,
                new=args.new,
                session=args.session,
                attach=not args.no_attach,
                no_claude=args.no_claude,
                machine_type_requested=requested,
            )
        if args.command == "status":
            st = connect.status(cfg)
            print(json.dumps(st, indent=2) if args.json else connect.format_status(st))
            return 0
        if args.command == "stop":
            print(f"VM {cfg.instance}: {gce.stop(cfg)}")
            return 0
    except (config_mod.ConfigError, gce.GcloudError, RuntimeError, TimeoutError) as e:
        print(f"cloud-coder: error: {e}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
