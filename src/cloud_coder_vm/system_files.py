"""Contents of the files cloud-coder installs on the VM.

Rendered by pure functions so the local CLI can compute the same bytes and
skip re-installing when nothing changed.
"""

import json
from dataclasses import asdict, dataclass

from cloud_coder_vm import paths


@dataclass(frozen=True)
class VmConfig:
    user: str
    grace_seconds: int
    workspace: str  # relative to the user's HOME
    auto_trust_workspace: bool
    swap_gb: int  # 0 = leave swap alone
    tools: list[str]  # names in dev_tools.TOOLS
    ignore_docker: bool  # do not let running containers block auto-stop


def render_config(config: VmConfig) -> str:
    return json.dumps(asdict(config), indent=2, sort_keys=True) + "\n"


def parse_config(text: str) -> VmConfig:
    return VmConfig(**json.loads(text))


def render_tmpfiles(user: str) -> str:
    return (
        f"d {paths.RUNTIME_DIR} 0755 {user} {user} -\n"
        f"d {paths.SESSION_STATE_DIR} 0755 {user} {user} -\n"
        f"f {paths.STATE_LOCK} 0644 {user} {user} -\n"
    )


def render_idle_service() -> str:
    return f"""[Unit]
Description=cloud-coder: stop the VM when all Claude Code sessions and tmux panes are idle
After=systemd-tmpfiles-setup.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {paths.AGENT_PYZ} idle-check
"""


def render_idle_timer() -> str:
    return f"""[Unit]
Description=cloud-coder: run the idle check every minute

[Timer]
OnBootSec=2min
OnUnitActiveSec=1min
AccuracySec=5s
Unit={paths.IDLE_SERVICE}

[Install]
WantedBy=timers.target
"""
