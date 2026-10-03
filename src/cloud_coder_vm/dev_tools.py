"""Optional developer tools installed on the VM, each the way its project recommends.

System tools go to /usr through apt; user tools go to the VM user's HOME. Both live
on the boot Persistent Disk, so they survive VM stops. A tool is installed only
when its binary is missing, so re-running the install is cheap.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path

APT = "apt-get -o DPkg::Lock::Timeout=600 -q"


@dataclass(frozen=True)
class Tool:
    name: str
    scope: str  # "system" (run as root) or "user" (run as the VM user)
    binary: str  # absolute, or relative to HOME for user tools
    script: str  # bash
    source: str  # where the method comes from


TOOLS: dict[str, Tool] = {
    tool.name: tool
    for tool in [
        Tool(
            "gh",
            "system",
            "/usr/bin/gh",
            "install -d -m 755 /etc/apt/keyrings\n"
            "curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg"
            " -o /etc/apt/keyrings/githubcli-archive-keyring.gpg\n"
            "chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg\n"
            'echo "deb [arch=$(dpkg --print-architecture)'
            " signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg]"
            ' https://cli.github.com/packages stable main"'
            " > /etc/apt/sources.list.d/github-cli.list\n"
            f"{APT} update && {APT} install -y gh\n",
            "https://github.com/cli/cli/blob/trunk/docs/install_linux.md",
        ),
        Tool(
            "node",
            "system",
            "/usr/bin/node",
            # NodeSource's setup_lts.x configures the apt repo of the current LTS line.
            "curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -\n"
            f"{APT} install -y nodejs\n"
            "if command -v corepack >/dev/null; then corepack enable; fi\n",
            "https://github.com/nodesource/distributions",
        ),
        Tool(
            "docker",
            "system",
            "/usr/bin/docker",
            "install -m 0755 -d /etc/apt/keyrings\n"
            "curl -fsSL https://download.docker.com/linux/ubuntu/gpg"
            " -o /etc/apt/keyrings/docker.asc\n"
            "chmod a+r /etc/apt/keyrings/docker.asc\n"
            "cat > /etc/apt/sources.list.d/docker.sources <<EOF\n"
            "Types: deb\n"
            "URIs: https://download.docker.com/linux/ubuntu\n"
            'Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")\n'
            "Components: stable\n"
            "Architectures: $(dpkg --print-architecture)\n"
            "Signed-By: /etc/apt/keyrings/docker.asc\n"
            "EOF\n"
            f"{APT} update && {APT} install -y docker-ce docker-ce-cli containerd.io"
            " docker-buildx-plugin docker-compose-plugin\n",
            "https://docs.docker.com/engine/install/ubuntu/",
        ),
        Tool(
            "rust",
            "user",
            ".cargo/bin/rustup",
            "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y\n",
            "https://www.rust-lang.org/tools/install",
        ),
        Tool(
            "uv",
            "user",
            ".local/bin/uv",
            "curl -LsSf https://astral.sh/uv/install.sh | sh\n",
            "https://docs.astral.sh/uv/getting-started/installation/",
        ),
    ]
}

DEFAULT_TOOLS = ("gh", "node", "rust", "docker", "uv")


def missing(names: list[str], scope: str, home: Path | None = None) -> list[Tool]:
    tools = [TOOLS[name] for name in names if TOOLS[name].scope == scope]
    base = home if scope == "user" else Path("/")
    return [t for t in tools if not (base / t.binary).exists()]


def install(tools: list[Tool]) -> None:
    for tool in tools:
        print(f"cloud-coder: installing {tool.name} ({tool.source})", flush=True)
        subprocess.run(["bash", "-euo", "pipefail", "-c", tool.script], check=True)
