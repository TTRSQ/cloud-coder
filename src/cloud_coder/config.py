"""cloud-coder configuration: built-in defaults <- config.yaml <- command line."""

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path

import yaml

from cloud_coder_vm.dev_tools import DEFAULT_TOOLS, TOOLS

DEFAULT_CONFIG_PATH = Path("~/.config/cloud-coder/config.yaml")
# Single source of the default machine type (README and tests refer to it).
DEFAULT_MACHINE_TYPE = "t2d-standard-8"


@dataclass(frozen=True)
class Config:
    # gcp
    project: str | None = None  # required: set in config.yaml or pass --project
    zone: str = "asia-northeast1-b"
    instance: str = "cloud-coder"
    machine_type: str = DEFAULT_MACHINE_TYPE
    disk_size_gb: int = 100
    disk_type: str = "pd-ssd"
    image_family: str = "ubuntu-2404-lts-amd64"
    image_project: str = "ubuntu-os-cloud"
    # ssh
    ssh_user: str = "coder"
    iap: bool = True
    # vm
    workspace: str = "git"  # clones: ~/<workspace>/<repo>
    worktrees: str = "git/wt"  # --new worktrees: ~/<worktrees>/<repo>-<n>
    idle_grace_minutes: int = 10
    swap_gb: int = 0  # 0 = do not create a swapfile
    tools: tuple[str, ...] = DEFAULT_TOOLS
    ignore_docker: bool = False  # running containers block auto-stop unless true
    ignore_ssh_sessions: bool = False  # interactive SSH logins block auto-stop unless true
    ssh_session_idle_minutes: int = 30
    cargo_disable_incremental: bool = True  # VM-wide Cargo default build.incremental = false
    # git
    github_https: bool = True  # use https://github.com/ for git@github.com: URLs on the VM
    # claude
    auto_trust_workspace: bool = True
    dotfiles_repo: str | None = None  # e.g. https://github.com/OWNER/dotClaude.git
    dotfiles_branch: str | None = None  # None = the repository's default branch
    dotfiles_install: str = "./install.sh"  # run in the clone after cloning


# YAML section -> {yaml key: Config field}
SECTIONS: dict[str, dict[str, str]] = {
    "gcp": {
        "project": "project",
        "zone": "zone",
        "instance": "instance",
        "machine_type": "machine_type",
        "disk_size_gb": "disk_size_gb",
        "disk_type": "disk_type",
        "image_family": "image_family",
        "image_project": "image_project",
    },
    "ssh": {"user": "ssh_user", "iap": "iap"},
    "vm": {
        "workspace": "workspace",
        "worktrees": "worktrees",
        "idle_grace_minutes": "idle_grace_minutes",
        "swap_gb": "swap_gb",
        "tools": "tools",
        "ignore_docker": "ignore_docker",
        "ignore_ssh_sessions": "ignore_ssh_sessions",
        "ssh_session_idle_minutes": "ssh_session_idle_minutes",
        "cargo_disable_incremental": "cargo_disable_incremental",
    },
    "git": {"github_https": "github_https"},
    "claude": {
        "auto_trust_workspace": "auto_trust_workspace",
        "dotfiles_repo": "dotfiles_repo",
        "dotfiles_branch": "dotfiles_branch",
        "dotfiles_install": "dotfiles_install",
    },
}


class ConfigError(Exception):
    pass


def config_path(explicit: str | None = None) -> Path:
    raw = explicit or os.environ.get("CLOUD_CODER_CONFIG") or str(DEFAULT_CONFIG_PATH)
    return Path(raw).expanduser()


def from_mapping(data: dict | None, base: Config | None = None) -> Config:
    base = base or Config()
    if not data:
        return base
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    types = {f.name: f.type for f in fields(Config)}
    values = {}
    for section, entries in data.items():
        if section not in SECTIONS:
            raise ConfigError(f"unknown config section {section!r}")
        if not isinstance(entries, dict):
            raise ConfigError(f"config section {section!r} must be a mapping")
        for key, value in entries.items():
            if key not in SECTIONS[section]:
                raise ConfigError(f"unknown config key {section}.{key}")
            name = SECTIONS[section][key]
            expected = types[name]
            if expected in (int, "int") and not (
                isinstance(value, int) and not isinstance(value, bool)
            ):
                raise ConfigError(f"{section}.{key} must be an integer")
            if expected in (bool, "bool") and not isinstance(value, bool):
                raise ConfigError(f"{section}.{key} must be true or false")
            if name == "tools":
                if not isinstance(value, list) or not all(v in TOOLS for v in value):
                    raise ConfigError(f"{section}.{key} must be a list of: {', '.join(TOOLS)}")
                value = tuple(value)
            values[name] = value
    return replace(base, **values)


def load(path: Path) -> Config:
    if not path.exists():
        return Config()
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}") from e
    return from_mapping(data)


def with_overrides(config: Config, **overrides) -> Config:
    return replace(config, **{k: v for k, v in overrides.items() if v is not None})
