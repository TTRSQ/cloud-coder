"""Preconditions shared by the adapters that let other programs drive the worker
(the MCP server and the HTTP API): which prompts may be sent, and when the VM is
ready for an operation. They raise the exceptions below; each adapter turns them
into its own error format.
"""

import threading
import unicodedata

from cloud_coder import connect, gce
from cloud_coder.config import Config


class PromptRejected(ValueError):
    """The prompt could make Claude Code run a shell command."""


class VmNotReady(Exception):
    """The VM was asked to start but is not ready for sessions yet."""

    def __init__(self, vm_action: str):
        super().__init__(f"the VM is not ready yet (VM {vm_action})")
        self.vm_action = vm_action


class VmNotRunning(Exception):
    """The VM is not running, and the operation does not start it."""

    def __init__(self, vm_status: str):
        super().__init__(f"the VM is {vm_status}")
        self.vm_status = vm_status


# An adapter serves requests concurrently; installing the agent twice at once would race.
# This serializes calls within one process only, not across `cloud-coder` processes.
_up_lock = threading.Lock()


def up_now(cfg: Config) -> connect.UpResult:
    """`connect.up` without waiting, one call at a time."""
    with _up_lock:
        return connect.up(cfg, wait=False)


def checked_prompt(text: str) -> str:
    """Claude Code runs input starting with ``!`` as a shell command, unchecked: refuse it,
    so that the adapters never run arbitrary commands on the VM. Control characters are
    refused too: an escape sequence could end the bracketed paste and type ``!`` as keys."""
    if text.lstrip().startswith("!"):
        raise PromptRejected("a prompt must not start with '!' (Claude Code's shell mode)")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in text):
        raise PromptRejected(
            "a prompt must not contain control characters other than newline and tab"
        )
    return text


def require_ready(cfg: Config) -> None:
    """Start the VM if needed; VmNotReady unless it is ready for sessions now."""
    result = up_now(cfg)
    if not result.ready:
        raise VmNotReady(result.vm_action)


def require_running(cfg: Config) -> None:
    """VmNotRunning unless the VM runs; never starts it."""
    vm = gce.describe(cfg)
    if vm.status != gce.RUNNING:
        raise VmNotRunning(vm.status)
