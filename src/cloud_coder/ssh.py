"""SSH / SCP to the worker VM through `gcloud compute ssh|scp` (external IP or IAP)."""

import logging
import subprocess
import time

from cloud_coder.config import Config

log = logging.getLogger(__name__)


def _target(cfg: Config) -> str:
    return f"{cfg.ssh_user}@{cfg.instance}"


def _common(cfg: Config) -> list[str]:
    args = [f"--zone={cfg.zone}", f"--project={cfg.project}", "--quiet"]
    if cfg.iap:
        args.append("--tunnel-through-iap")
    return args


def ssh_command(
    cfg: Config,
    command: str,
    *,
    tty: bool = False,
    forward_agent: bool = False,
    connect_timeout: int = 20,
) -> list[str]:
    ssh_flags = ["-o", f"ConnectTimeout={connect_timeout}", "-o", "ServerAliveInterval=30"]
    if tty:
        ssh_flags.append("-t")
    if forward_agent:
        ssh_flags.append("-A")
    return [
        "gcloud",
        "compute",
        "ssh",
        _target(cfg),
        *_common(cfg),
        f"--command={command}",
        "--",
        *ssh_flags,
    ]


def run(
    cfg: Config, command: str, *, forward_agent: bool = False, capture: bool = True
) -> subprocess.CompletedProcess:
    """Run ``command`` on the VM. Without ``capture`` its output goes to our stderr, never
    to our stdout, which carries machine-readable output (JSON, the MCP stdio stream)."""
    return subprocess.run(
        ssh_command(cfg, command, forward_agent=forward_agent),
        capture_output=capture,
        stdout=None if capture else 2,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def scp(cfg: Config, local_path: str, remote_path: str) -> None:
    subprocess.run(
        ["gcloud", "compute", "scp", local_path, f"{_target(cfg)}:{remote_path}", *_common(cfg)],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )


def reachable(cfg: Config) -> bool:
    return run(cfg, "true").returncode == 0


def wait_ready(cfg: Config, timeout: float = 300) -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = run(cfg, "true")
        if result.returncode == 0:
            return
        if time.monotonic() > deadline:
            raise TimeoutError(f"SSH to {cfg.instance} not ready: {result.stderr.strip()[-500:]}")
        log.info("waiting for SSH...")
        time.sleep(5)


def attach_tmux(cfg: Config, tmux_session: str) -> int:
    command = f"tmux attach-session -t ={tmux_session}"
    return subprocess.run(ssh_command(cfg, command, tty=True)).returncode
