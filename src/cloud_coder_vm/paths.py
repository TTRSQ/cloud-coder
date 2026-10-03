"""Fixed filesystem locations used on the VM.

Runtime state lives on tmpfs (/run) and disappears on shutdown on purpose.
The session registry lives under HOME, which is on the Persistent Disk.
"""

from pathlib import Path

INSTALL_DIR = Path("/opt/cloud-coder")
AGENT_PYZ = INSTALL_DIR / "cloud-coder-vm.pyz"
CONFIG_PATH = Path("/etc/cloud-coder/config.json")

RUNTIME_DIR = Path("/run/cloud-coder")
SESSION_STATE_DIR = RUNTIME_DIR / "sessions"
STATE_LOCK = RUNTIME_DIR / "state.lock"
IDLE_SINCE = RUNTIME_DIR / "idle_since"
HOOK_LOG = RUNTIME_DIR / "hook.log"
PROMPT_DIR = RUNTIME_DIR / "prompts"
BUSY_DIR = RUNTIME_DIR / "busy"

TMPFILES_CONF = Path("/etc/tmpfiles.d/cloud-coder.conf")
SYSTEMD_DIR = Path("/etc/systemd/system")
IDLE_SERVICE = "cloud-coder-idle-check.service"
IDLE_TIMER = "cloud-coder-idle-check.timer"

HOOK_COMMAND = f"/usr/bin/python3 {AGENT_PYZ} hook"


# Hooks live in Claude Code's managed settings, so ~/.claude/settings.json (often a
# dotfiles symlink) is never written. https://code.claude.com/docs/en/managed-settings
MANAGED_SETTINGS_FILE = Path("/etc/claude-code/managed-settings.d/50-cloud-coder.json")
# where clones lived before workspace defaulted to ~/git; sessions there keep working
LEGACY_WORKSPACE = "workspace"


def registry_path(home: Path) -> Path:
    return home / ".local/share/cloud-coder/sessions.json"


def claude_settings_path(home: Path) -> Path:
    return home / ".claude/settings.json"


def claude_global_config_path(home: Path) -> Path:
    return home / ".claude.json"


def claude_bin(home: Path) -> Path:
    return home / ".local/bin/claude"
