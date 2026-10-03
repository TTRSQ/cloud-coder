"""Ship the VM agent (cloud_coder_vm as a zipapp) and install it when it changed.

Like DevPod's agent injection: on every up/connect compare hashes over SSH,
and only copy + install when the agent or its config differ.
"""

import hashlib
import io
import shlex
import sys
import tempfile
import zipfile
from importlib import resources
from pathlib import Path

from cloud_coder import ssh
from cloud_coder.config import Config
from cloud_coder_vm import paths
from cloud_coder_vm.system_files import VmConfig, render_config

_FIXED_DATE = (2020, 1, 1, 0, 0, 0)
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
        auto_trust_workspace=cfg.auto_trust_workspace,
        swap_gb=cfg.swap_gb,
        tools=list(cfg.tools),
        ignore_docker=cfg.ignore_docker,
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


CHECK_COMMAND = (
    f"sha256sum {paths.AGENT_PYZ} {paths.CONFIG_PATH} 2>/dev/null; "
    f"test -x ~/.local/bin/claude && echo claude-installed; "
    f"test -f ~/.claude/settings.json && grep -q {shlex.quote(str(paths.AGENT_PYZ))} "
    f"~/.claude/settings.json && echo hooks-installed; true"
)


def is_current(check_output: str, pyz_sha: str, config_sha: str) -> bool:
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
    )


def ensure_installed(cfg: Config) -> bool:
    """Returns True when the agent was (re)installed."""
    pyz = build_pyz()
    config_text = render_config(vm_config(cfg))
    pyz_sha, config_sha = sha256(pyz), sha256(config_text.encode())

    check = ssh.run(cfg, CHECK_COMMAND)
    if check.returncode == 0 and is_current(check.stdout, pyz_sha, config_sha):
        return False

    print("cloud-coder: installing the VM agent", file=sys.stderr, flush=True)
    remote = f"/tmp/cloud-coder-vm-{pyz_sha[:12]}.pyz"
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / "cloud-coder-vm.pyz"
        local.write_bytes(pyz)
        ssh.scp(cfg, str(local), remote)
    command = " && ".join(
        [
            shlex.join(["sudo", "python3", remote, "install-system", "--config", config_text]),
            shlex.join(["python3", str(paths.AGENT_PYZ), "install-user"]),
            shlex.join(["rm", "-f", remote]),
        ]
    )
    result = ssh.run(cfg, command, capture=False)
    if result.returncode != 0:
        raise RuntimeError("installing the VM agent failed (see output above)")
    return True
