"""MCP server: the worker VM and its Claude Code sessions as tools for any MCP client.

The server is built for one target VM, fixed by the configuration it is created with;
no tool takes a project, zone or instance, and none runs arbitrary commands. Tools
return quickly: starting or stopping the VM is only requested, and the caller polls.
The server does not depend on a transport: `cloud-coder mcp` serves it over stdio, and
the HTTP API serves it at /mcp (Streamable HTTP) behind bearer tokens and OAuth.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from cloud_coder import config as config_mod
from cloud_coder import connect, gce, guards
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
    guards.PromptRejected,
    config_mod.ConfigError,
    gce.GcloudError,
    connect.AgentError,
    RuntimeError,
    TimeoutError,
    OSError,
)


@contextmanager
def tool_errors() -> Iterator[None]:
    try:
        yield
    except guards.VmNotReady as e:
        raise ToolError(f"{e}; call `up` until it reports ready, then retry") from e
    except guards.VmNotRunning as e:
        raise ToolError(f"{e}; nothing to read") from e
    except EXPECTED_ERRORS as e:
        raise ToolError(str(e)) from e


def build_server(
    cfg: Config, *, auth: AuthSettings | None = None, token_verifier: TokenVerifier | None = None
) -> MCPServer:
    """``auth`` and ``token_verifier`` protect the HTTP transports: then tools that are not
    read-only need a token with the write scope. stdio has no tokens and allows every tool.
    """
    server = MCPServer(
        "cloud-coder", instructions=INSTRUCTIONS, auth=auth, token_verifier=token_verifier
    )

    def require_write() -> None:
        if auth is None:
            return
        token = get_access_token()
        if token is None or guards.WRITE not in token.scopes:
            raise ToolError("this token may call only the read-only tools (status, read_session)")

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
        require_write()
        with tool_errors():
            return asdict(guards.up_now(cfg))

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
        require_write()
        with tool_errors():
            if prompt is not None:
                guards.checked_prompt(prompt)
            guards.require_ready(cfg)
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
        require_write()
        with tool_errors():
            guards.checked_prompt(text)
            guards.require_ready(cfg)
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
            guards.require_running(cfg)
            return connect.read_session(cfg, session, lines)

    @server.tool(annotations=STOPS)
    def stop() -> dict:
        """Stop the VM now (its disk is kept) and return without waiting. Interrupts
        any running work; the VM also stops by itself once every session is idle."""
        require_write()
        with tool_errors():
            return {"vm": gce.stop(cfg, wait=False)}

    return server
