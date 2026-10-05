"""A deadline for the external commands (gcloud, its ssh and scp) of one operation.

The MCP server sets one per tool call so that no call outlasts the client's limit (about
60 seconds per tool call for ChatGPT). Outside a deadline commands run as long as they
take, as the command line needs (waiting for a VM, a first agent install, a long clone).
"""

import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_deadline: ContextVar[float | None] = ContextVar("deadline", default=None)


@contextmanager
def within(seconds: float) -> Iterator[None]:
    """Commands run in this context (and this thread) must finish within ``seconds``."""
    token = _deadline.set(time.monotonic() + seconds)
    try:
        yield
    finally:
        _deadline.reset(token)


def run(args: list[str], **kwargs) -> subprocess.CompletedProcess:
    """``subprocess.run`` bounded by the current deadline; TimeoutError when it passes."""
    deadline = _deadline.get()
    if deadline is None:
        return subprocess.run(args, **kwargs)
    command = " ".join(args[:3])
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"out of time before running `{command}`")
    try:
        return subprocess.run(args, timeout=remaining, **kwargs)
    except subprocess.TimeoutExpired as e:
        # Only the gcloud process is killed: what it started on the VM may still finish.
        raise TimeoutError(
            f"`{command}` did not finish in time; what it started may still complete, "
            "so check the state before retrying"
        ) from e
