"""MCP server: the worker VM and its Claude Code sessions as tools for any MCP client.

The server is built for one target VM, fixed by the configuration it is created with;
no tool takes a project, zone or instance, and none runs arbitrary commands. Tools
return quickly: starting or stopping the VM is only requested, and the caller polls.
The server does not depend on a transport: `cloud-coder mcp` serves it over stdio.
"""

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from cloud_coder import config as config_mod
from cloud_coder import connect, gce
from cloud_coder.config import Config

INSTRUCTIONS = """\
Controls one cloud-coder worker: a GCE VM that runs Claude Code sessions in tmux and
stops itself when every session is idle.
- `status` shows the VM, its sessions and each Claude Code's state (BUSY / READY / IDLE).
- `up` starts the VM and returns at once; call it again until `ready` is true.
- `start_session` opens (or returns to) a session for a repository and can pass a first
  prompt. `send_prompt` gives an existing session a new instruction; it is refused while
  Claude Code is BUSY. `read_session` returns the session's recent screen text, to follow
  progress or read Claude Code's answer.
- `stop` stops the VM at once, interrupting any work; normally let it stop itself.
"""

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
STARTS = ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ACTS = ToolAnnotations(destructive_hint=False, open_world_hint=False)
STOPS = ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=False)

# Failures the tools expect; anything else is a bug and reaches the client only as
# "Error executing tool".
EXPECTED_ERRORS = (
    config_mod.ConfigError,
    gce.GcloudError,
    connect.AgentError,
    RuntimeError,
    TimeoutError,
    OSError,
)


# The SDK runs tools concurrently; installing the agent twice at once would race.
_up_lock = threading.Lock()


def up_now(cfg: Config) -> connect.UpResult:
    with _up_lock:
        return connect.up(cfg, wait=False)


def checked_prompt(text: str) -> str:
    """Claude Code runs input starting with ``!`` as a shell command, unchecked: refuse it,
    so that the tools never run arbitrary commands on the VM."""
    if text.lstrip().startswith("!"):
        raise ToolError("a prompt must not start with '!' (Claude Code's shell mode)")
    return text


@contextmanager
def tool_errors() -> Iterator[None]:
    try:
        yield
    except EXPECTED_ERRORS as e:
        raise ToolError(str(e)) from e


def require_ready(cfg: Config) -> None:
    result = up_now(cfg)
    if not result.ready:
        raise ToolError(
            f"the VM is not ready yet (VM {result.vm_action}); "
            "call `up` until it reports ready, then retry"
        )


def build_server(cfg: Config) -> MCPServer:
    server = MCPServer("cloud-coder", instructions=INSTRUCTIONS)

    @server.tool(annotations=READ_ONLY)
    def status() -> dict:
        """State of the VM and, when it runs, its sessions and auto-stop."""
        with tool_errors():
            return connect.status(cfg)

    @server.tool(annotations=STARTS)
    def up() -> dict:
        """Start the VM if it is not running and return without waiting. When it runs,
        also install or update the cloud-coder agent on it (the first install can take
        several minutes). Call again until `ready` is true."""
        with tool_errors():
            return asdict(up_now(cfg))

    @server.tool(annotations=ACTS)
    def start_session(
        repo: Annotated[
            str | None,
            Field(
                description="git URL to clone, or the name of a repository already on the "
                "VM. Omit to return to the most recently used session."
            ),
        ] = None,
        new: Annotated[
            bool,
            Field(description="start another session for the repo in a new git worktree"),
        ] = False,
        session: Annotated[
            str | None, Field(description="return to this session (name from `status`)")
        ] = None,
        prompt: Annotated[
            str | None,
            Field(
                description="first instruction for Claude Code; for a running Claude Code "
                "it is sent only when it is READY or IDLE"
            ),
        ] = None,
    ) -> dict:
        """Make sure a session (git checkout, tmux session and Claude Code) exists and
        runs, and return its name. Requires a ready VM (see `up`)."""
        if prompt is not None:
            checked_prompt(prompt)
        with tool_errors():
            require_ready(cfg)
            return connect.launch(
                cfg, repo, new=new, session=session, prompt=prompt, forward_agent=False
            )

    @server.tool(annotations=ACTS)
    def send_prompt(
        session: Annotated[str, Field(description="session name from `status`")],
        text: Annotated[str, Field(description="the instruction for Claude Code")],
    ) -> dict:
        """Type an instruction into the session's Claude Code and submit it. Refused
        unless Claude Code is READY or IDLE; if Claude Code is not running it is
        started (or resumed) with this instruction."""
        checked_prompt(text)
        with tool_errors():
            require_ready(cfg)
            return connect.launch(cfg, None, session=session, prompt=text, forward_agent=False)

    @server.tool(annotations=READ_ONLY)
    def read_session(
        session: Annotated[str, Field(description="session name from `status`")],
        lines: Annotated[
            int, Field(ge=1, le=2000, description="how many of the last lines to return")
        ] = 200,
    ) -> dict:
        """Recent text of the session's Claude Code screen (scrollback included) and
        Claude Code's state. Does not start the VM."""
        with tool_errors():
            vm = gce.describe(cfg)
            if vm.status != gce.RUNNING:
                raise ToolError(f"the VM is {vm.status}; nothing to read")
            return connect.read_session(cfg, session, lines)

    @server.tool(annotations=STOPS)
    def stop() -> dict:
        """Stop the VM now (its disk is kept) and return without waiting. Interrupts
        any running work; the VM also stops by itself once every session is idle."""
        with tool_errors():
            return {"vm": gce.stop(cfg, wait=False)}

    return server
