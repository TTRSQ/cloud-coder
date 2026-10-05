"""Ship the VM agent (cloud_coder_vm as a zipapp) and install it when it changed.

Like DevPod's agent injection: on every up/connect compare hashes over SSH,
and only copy + install when the agent or its config differ.
"""

import hashlib
import io
import logging
import shlex
import tempfile
import zipfile
from importlib import resources
from pathlib import Path

from cloud_coder import ssh
from cloud_coder.config import Config
from cloud_coder_vm import paths
from cloud_coder_vm.system_files import VmConfig, render_config

_FIXED_DATE = (2020, 1, 1, 0, 0, 0)
log = logging.getLogger(__name__)

_MAIN = "import sys\nfrom cloud_coder_vm.cli import main\nsys.exit(main())\n"


def build_pyz() -> bytes:
    """Byte-for-byte reproducible zipapp of the cloud_coder_vm package."""
    package = resources.files("cloud_coder_vm")
    files = {"__main__.py": _MAIN.encode()}
    for entry in package.iterdir():
        if entry.name.endswith(".py"):
            files[f"cloud_coder_vm/{entry.name}"] = entry.read_bytes()
    buffer = io.BytesIO()
    buffer.write(b"#!/usr/bin/env python3\n")
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in sorted(files):
            info = zipfile.ZipInfo(name, date_time=_FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            zf.writestr(info, files[name])
    return buffer.getvalue()


def vm_config(cfg: Config) -> VmConfig:
    return VmConfig(
        user=cfg.ssh_user,
        grace_seconds=cfg.idle_grace_minutes * 60,
        workspace=cfg.workspace,
        worktrees=cfg.worktrees,
        auto_trust_workspace=cfg.auto_trust_workspace,
        swap_gb=cfg.swap_gb,
        tools=list(cfg.tools),
        ignore_docker=cfg.ignore_docker,
        ignore_ssh_sessions=cfg.ignore_ssh_sessions,
        ssh_session_idle_minutes=cfg.ssh_session_idle_minutes,
        github_https=cfg.github_https,
        dotfiles_repo=cfg.dotfiles_repo,
        dotfiles_branch=cfg.dotfiles_branch,
        dotfiles_install=cfg.dotfiles_install,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# The detached install of `start_install` on the VM, relative to the VM user's HOME: it
# holds INSTALL_LOCK while it runs, then writes its exit status to INSTALL_STATUS.
INSTALL_LOCK = ".cloud-coder-install.lock"
INSTALL_STATUS = ".cloud-coder-install.status"
INSTALL_LOG = "cloud-coder-install.log"

INSTALLED = "installed"
INSTALLING = "installing"
FAILED = "failed"
MISSING = "missing"


def check_command(cfg: Config) -> str:
    """Prints the installed hashes plus a marker for each thing that must be present,
    and the state of a detached install."""
    parts = [
        f"sha256sum {paths.AGENT_PYZ} {paths.CONFIG_PATH} 2>/dev/null",
        "test -x ~/.local/bin/claude && echo claude-installed",
        f"test -f {paths.MANAGED_SETTINGS_FILE} && echo hooks-installed",
        f"flock -n ~/{INSTALL_LOCK} true || echo install-running",
        f"sed 's/^/install-exit-/' ~/{INSTALL_STATUS} 2>/dev/null",
    ]
    if cfg.dotfiles_repo:
        parts.append(f"test -f ~/{paths.DOTFILES_STAMP} && echo dotfiles-installed")
    return "; ".join(parts) + "; true"


def is_current(check_output: str, pyz_sha: str, config_sha: str, dotfiles: bool = False) -> bool:
    hashes = {}
    for line in check_output.splitlines():
        parts = line.split()
        if len(parts) == 2:
            hashes[parts[1]] = parts[0]
    return (
        hashes.get(str(paths.AGENT_PYZ)) == pyz_sha
        and hashes.get(str(paths.CONFIG_PATH)) == config_sha
        and "claude-installed" in check_output
        and "hooks-installed" in check_output
        and (not dotfiles or "dotfiles-installed" in check_output)
    )


def ensure_installed(cfg: Config) -> bool:
    """Install the agent unless it is current, waiting for the install (several minutes
    the first time). Returns True when the agent was (re)installed."""
    if install_state(cfg) == INSTALLED:
        return False
    remote = upload(cfg)
    result = ssh.run(cfg, f"{install_command(cfg, remote)}; exit $status", capture=False)
    if result.returncode != 0:
        raise RuntimeError("installing the VM agent failed (see output above)")
    return True


def install_state(cfg: Config) -> str:
    """INSTALLED when the VM has this agent and its config; otherwise whether a detached
    install is running (INSTALLING), ended in failure (FAILED) or is not there (MISSING).
    One quick SSH command."""
    pyz_sha = sha256(build_pyz())
    config_sha = sha256(render_config(vm_config(cfg)).encode())
    check = ssh.run(cfg, check_command(cfg))
    out = check.stdout
    if check.returncode == 0 and is_current(
        out, pyz_sha, config_sha, dotfiles=bool(cfg.dotfiles_repo)
    ):
        return INSTALLED
    if "install-running" in out:
        return INSTALLING
    if "install-exit-" in out and "install-exit-0" not in out:
        return FAILED
    return MISSING


def start_install(cfg: Config) -> None:
    """Copy the agent to the VM and start installing it there, detached from this SSH
    connection: it runs on even when this process ends. Follow it with `install_state`."""
    remote = upload(cfg)
    script = f"{install_command(cfg, remote)}; echo $status > {INSTALL_STATUS}"
    detached = shlex.join(["nohup", "flock", "-n", INSTALL_LOCK, "sh", "-c", script])
    # only the install goes to the background, with no fd left on the SSH channel
    command = f"cd && rm -f {INSTALL_STATUS} && {{ {detached} > {INSTALL_LOG} 2>&1 < /dev/null & }}"
    result = ssh.run(cfg, command)
    if result.returncode != 0:
        raise RuntimeError(f"starting the VM agent install failed: {result.stderr.strip()[-500:]}")


def forget_failed_install(cfg: Config) -> None:
    """Clear a FAILED state, so that the next `install_state` is MISSING."""
    ssh.run(cfg, f"rm -f ~/{INSTALL_STATUS}")


def upload(cfg: Config) -> str:
    """Copy the agent to the VM; returns its path there, relative to the user's HOME
    (not a world-writable, predictable /tmp path)."""
    pyz = build_pyz()
    remote = f"cloud-coder-vm-{sha256(pyz)[:12]}.pyz"
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "cloud-coder-vm.pyz"
        local.write_bytes(pyz)
        ssh.scp(cfg, str(local), remote)
    return remote


def install_command(cfg: Config, remote: str) -> str:
    """Shell commands that install the uploaded agent (the first time takes several
    minutes), remove the uploaded copy whether or not that succeeded, and leave the
    install's exit status in ``$status``."""
    config_text = render_config(vm_config(cfg))
    steps = " && ".join(
        [
            shlex.join(["sudo", "python3", remote, "install-system", "--config", config_text]),
            shlex.join(["python3", str(paths.AGENT_PYZ), "install-user"]),
        ]
    )
    return f"{steps}; status=$?; rm -f {shlex.quote(remote)}"
