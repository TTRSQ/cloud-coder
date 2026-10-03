"""Install / update cloud-coder on the VM. Idempotent; safe to run on every connect.

``install-system`` runs as root (systemd units, tmpfiles, packages).
``install-user`` runs as the VM user (Claude Code, hooks, workspace).
"""

import copy
import json
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from cloud_coder_vm import dev_tools, paths, system_files
from cloud_coder_vm.state_lock import write_atomic
from cloud_coder_vm.system_files import VmConfig

HOOK_EVENTS: list[tuple[str, str | None]] = [
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("Stop", None),
    ("StopFailure", None),
    ("Notification", "idle_prompt"),
    ("SessionEnd", None),
]
# Always installed: what cloud-coder itself and most toolchains (cargo, node-gyp) need.
BASE_APT_PACKAGES = ["tmux", "git", "curl", "ca-certificates", "build-essential"]
CLAUDE_INSTALLER = "curl -fsSL https://claude.ai/install.sh | bash"


@contextmanager
def installing() -> Iterator[None]:
    """Mark an install in progress so the idle check does not shut the VM down under it."""
    marker = paths.INSTALL_MARKER
    with suppress(OSError):  # runtime dir not created yet (first install)
        marker.write_text(f"{os.getpid()}\n")
    try:
        yield
    finally:
        marker.unlink(missing_ok=True)


def install_in_progress(marker: Path = paths.INSTALL_MARKER) -> bool:
    try:
        pid = int(marker.read_text().strip())
    except (OSError, ValueError):
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def merge_hooks(settings: dict, command: str = paths.HOOK_COMMAND) -> dict:
    """Return settings with exactly one cloud-coder handler per event, keeping all others."""
    merged = copy.deepcopy(settings)
    hooks = merged.setdefault("hooks", {})
    for event in list(hooks):
        groups = []
        for group in hooks[event]:
            handlers = [h for h in group.get("hooks", []) if h.get("command") != command]
            if handlers:
                groups.append({**group, "hooks": handlers})
        if groups:
            hooks[event] = groups
        else:
            del hooks[event]
    for event, matcher in HOOK_EVENTS:
        group: dict = {"hooks": [{"type": "command", "command": command, "timeout": 10}]}
        if matcher:
            group = {"matcher": matcher, **group}
        hooks.setdefault(event, []).append(group)
    return merged


def _write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text() == text:
        return False
    write_atomic(path, text)
    return True


SWAPFILE = Path("/swapfile")
FSTAB = Path("/etc/fstab")


def ensure_swapfile(size_gb: int) -> None:
    """Create and enable /swapfile once. Never resizes or removes an existing one."""
    if size_gb <= 0 or SWAPFILE.exists():
        return
    subprocess.run(["fallocate", "-l", f"{size_gb}G", str(SWAPFILE)], check=True)
    SWAPFILE.chmod(0o600)
    subprocess.run(["mkswap", str(SWAPFILE)], check=True, capture_output=True)
    subprocess.run(["swapon", str(SWAPFILE)], check=True)
    if str(SWAPFILE) not in FSTAB.read_text():
        with open(FSTAB, "a") as fstab:
            fstab.write(f"{SWAPFILE} none swap sw 0 0\n")


def _apt_installed(package: str) -> bool:
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${Status}", package], capture_output=True, text=True
    )
    return result.stdout.strip() == "install ok installed"


def install_system(pyz_source: Path, config: VmConfig) -> None:
    os.environ["DEBIAN_FRONTEND"] = "noninteractive"
    missing = [pkg for pkg in BASE_APT_PACKAGES if not _apt_installed(pkg)]
    if missing:
        lock = ["-o", "DPkg::Lock::Timeout=600"]
        subprocess.run(["apt-get", *lock, "update", "-q"], check=True)
        subprocess.run(["apt-get", *lock, "install", "-y", "-q", *missing], check=True)

    paths.INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    if pyz_source.resolve() != paths.AGENT_PYZ.resolve():
        tmp = paths.AGENT_PYZ.with_suffix(".tmp")
        shutil.copyfile(pyz_source, tmp)
        tmp.chmod(0o755)
        tmp.replace(paths.AGENT_PYZ)

    _write_if_changed(paths.CONFIG_PATH, system_files.render_config(config))
    _write_if_changed(paths.TMPFILES_CONF, system_files.render_tmpfiles(config.user))
    _write_if_changed(paths.SYSTEMD_DIR / paths.IDLE_SERVICE, system_files.render_idle_service())
    _write_if_changed(paths.SYSTEMD_DIR / paths.IDLE_TIMER, system_files.render_idle_timer())

    ensure_swapfile(config.swap_gb)
    dev_tools.install(dev_tools.missing(config.tools, "system"))
    if "docker" in config.tools:
        # takes effect for new logins; launch starts tmux sessions with the group (see launch)
        subprocess.run(["usermod", "-aG", "docker", config.user], check=True)
    subprocess.run(["systemd-tmpfiles", "--create", str(paths.TMPFILES_CONF)], check=True)
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    subprocess.run(["systemctl", "enable", "--now", paths.IDLE_TIMER], check=True)


def install_user(config: VmConfig, home: Path) -> None:
    (home / config.workspace).mkdir(parents=True, exist_ok=True)

    settings_path = paths.claude_settings_path(home)
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    merged = merge_hooks(settings)
    if merged != settings:
        write_atomic(settings_path, json.dumps(merged, indent=2) + "\n", mode=0o600)

    if not paths.claude_bin(home).exists():
        subprocess.run(["bash", "-c", CLAUDE_INSTALLER], check=True)
    dev_tools.install(dev_tools.missing(config.tools, "user", home))
