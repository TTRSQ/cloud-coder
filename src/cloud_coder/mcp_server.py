"""MCP server: the worker VM and its Claude Code sessions as tools for any MCP client.

The server is built for one target VM, fixed by the configuration it is created with;
no tool takes a project, zone or instance, and none runs arbitrary commands. Tools
return quickly: starting or stopping the VM is only requested, and the caller polls;
every call is bounded by a deadline. Claude Code's work takes far longer than an LLM
client's turn, so the instructions and results tell the client to hand control back to
the user once work has started, not to wait for it, and re-reads of a session found
BUSY moments ago are answered without reaching the VM.
The server does not depend on a transport: `cloud-coder mcp` serves it over stdio, and
the HTTP API serves it at /mcp (Streamable HTTP) behind bearer tokens and OAuth.
"""

import logging
import threading
import time
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
from cloud_coder import connect, deadline, gce, guards
from cloud_coder.config import Config
from cloud_coder_vm.session_state import BUSY

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Controls one cloud-coder worker: a GCE VM that runs Claude Code sessions in tmux and
stops itself when every session is idle.
Claude Code's work takes minutes to hours, far longer than one turn of yours. Once
`start_session` or `send_prompt` succeeds, tell the user the work has started and end
your turn. Do not poll `status` or `read_session` to wait for BUSY to end; call them only
when the user asks how the work is going or for its result. When `send_prompt` is
refused because Claude Code is BUSY, do not resend it; tell the user.
- `status` shows the VM, its sessions and each Claude Code's state (BUSY / READY / IDLE).
- `up` starts the VM and returns at once. While `ready` is false the VM is starting
  (about a minute) or the agent is being installed (the first time, several minutes):
  call it again after a while, and if it is still not ready tell the user and stop.
- `start_session` opens (or returns to) a session for a repository and can pass a first
  prompt. `send_prompt` gives an existing session a new instruction; it is refused while
  Claude Code is BUSY. `read_session` returns the session's recent screen text: Claude
  Code's answer, its progress, or a question it is waiting on.
- `stop` stops the VM at once, interrupting any work; normally let it stop itself.
"""

# The result fields that tell the client what to do next.
STARTED_NEXT = (
    "Claude Code is working on the prompt; this takes minutes to hours. Tell the user it "
    "has started and end your turn. Do not call status or read_session to wait for it; "
    "call them only when the user asks."
)
SESSION_READY_NEXT = (
    "The session is ready and Claude Code has no new work. Tell the user; give it work "
    "with send_prompt when the user asks."
)
BUSY_NOTE = (
    "Claude Code is still working (BUSY). Report the progress so far to the user and end "
    "your turn; do not call status or read_session again in this turn."
)
PROMPT_NOT_RESENT = (
    "Claude Code is still working on an earlier instruction: do not resend this prompt; "
    "tell the user and end your turn"
)

# A tool call ends within this. Clients cut tool calls off after a limit of their own
# (unpublished for ChatGPT): this keeps a stuck gcloud or ssh from outlasting it by far,
# while leaving room for the slow steps of a normal call (VM start, SSH to a new VM).
CALL_SECONDS = 120
# A session (or status) found BUSY is not read again from the VM within this window.
REREAD_SECONDS = 60


class BusyReads:
    """When `status` or `read_session` last found Claude Code BUSY, per target, so that a
    client polling for BUSY to end is answered without reaching the VM."""

    def __init__(self, window: float = REREAD_SECONDS):
        self.window = window
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def seconds_since(self, target: str) -> float | None:
        """Seconds since ``target`` was found BUSY, when that is within the window."""
        with self._lock:
            seen = self._seen.get(target)
        if seen is None:
            return None
        elapsed = time.monotonic() - seen
        return elapsed if elapsed < self.window else None

    def record(self, target: str, busy: bool) -> None:
        with self._lock:
            if busy:
                self._seen[target] = time.monotonic()
            else:
                self._seen.pop(target, None)

    def clear(self) -> None:
        """A write tool may have changed what the targets would show."""
        with self._lock:
            self._seen.clear()

    def not_reread(self, elapsed: float) -> dict:
        return {
            "rechecked": False,
            "seconds_since_check": round(elapsed),
            "note": (
                f"Not read again: Claude Code was BUSY {elapsed:.0f} s ago, and its work "
                "takes minutes to hours. Stop polling: report the progress to the user and "
                "end your turn. A fresh read is possible "
                f"{self.window - elapsed:.0f} s from now, when the user asks."
            ),
        }


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
def tool_call(name: str) -> Iterator[None]:
    """Run a tool's body within the call deadline, log it, and turn the expected failures
    into tool errors the client can read."""
    started = time.monotonic()
    outcome = "failed"
    try:
        with deadline.within(CALL_SECONDS):
            yield
        outcome = "ok"
    except guards.VmNotReady as e:
        raise ToolError(f"{e}; call `up` until it reports ready, then retry") from e
    except guards.VmNotRunning as e:
        raise ToolError(f"{e}; nothing to read") from e
    except connect.AgentError as e:
        if str(e).endswith("is BUSY; prompt not sent"):
            raise ToolError(f"{e}. {PROMPT_NOT_RESENT}") from e
        raise ToolError(str(e)) from e
    except EXPECTED_ERRORS as e:
        raise ToolError(str(e)) from e
    finally:
        log.info(f"MCP tool {name}: {outcome} in {time.monotonic() - started:.1f}s")


def build_server(
    cfg: Config, *, auth: AuthSettings | None = None, token_verifier: TokenVerifier | None = None
) -> MCPServer:
    """``auth`` and ``token_verifier`` protect the HTTP transports: then tools that are not
    read-only need a token with the write scope. stdio has no tokens and allows every tool.
    """
    server = MCPServer(
        "cloud-coder", instructions=INSTRUCTIONS, auth=auth, token_verifier=token_verifier
    )
    busy_reads = BusyReads()

    @contextmanager
    def write_call(name: str) -> Iterator[None]:
        """`tool_call` for the tools that are not read-only: over HTTP they need a token
        with the write scope, and what they change makes earlier BUSY reads stale."""
        with tool_call(name):
            if auth is not None:
                token = get_access_token()
                if token is None or guards.WRITE not in token.scopes:
                    raise ToolError(
                        "this token may call only the read-only tools (status, read_session)"
                    )
            busy_reads.clear()
            yield

    def started(result: dict) -> dict:
        next_step = STARTED_NEXT if "prompt" in result else SESSION_READY_NEXT
        return {**result, "accepted": True, "next": next_step}

    @server.tool(annotations=READ_ONLY)
    def status() -> dict:
        """State of the VM and, when it runs, its sessions and auto-stop. Call it when the
        user asks; never poll it to wait for a BUSY Claude Code."""
        with tool_call("status"):
            elapsed = busy_reads.seconds_since("status")
            if elapsed is not None:
                return busy_reads.not_reread(elapsed)
            st = connect.status(cfg)
            busy = any(s.get("claude_state") == BUSY for s in st.get("sessions", []))
            busy_reads.record("status", busy)
            return {**st, "note": BUSY_NOTE} if busy else st

    @server.tool(annotations=STARTS)
    def up() -> dict:
        """Start the VM if it is not running and return without waiting. When it runs,
        also install or update the cloud-coder agent on it, in the background (the first
        install takes several minutes). Call again after a while until `ready` is true."""
        with write_call("up"):
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
        runs, and return its name. Requires a ready VM (see `up`). With a prompt, Claude
        Code works on it for minutes to hours: tell the user it has started and end your
        turn instead of waiting."""
        with write_call("start_session"):
            if prompt is not None:
                guards.checked_prompt(prompt)
            guards.require_ready(cfg)
            return started(
                connect.launch(
                    cfg, repo, new=new, session=session, prompt=prompt, forward_agent=False
                )
            )

    @server.tool(annotations=ACTS)
    def send_prompt(
        session: Annotated[str, Field(description="session name from `status`")],
        text: Annotated[str, Field(description="the instruction for Claude Code")],
    ) -> dict:
        """Type an instruction into the session's Claude Code and submit it. Refused
        unless Claude Code is READY or IDLE (do not resend it then); if Claude Code is not
        running it is started (or resumed) with this instruction. Claude Code works on it
        for minutes to hours: tell the user it has started and end your turn."""
        with write_call("send_prompt"):
            guards.checked_prompt(text)
            guards.require_ready(cfg)
            return started(
                connect.launch(cfg, None, session=session, prompt=text, forward_agent=False)
            )

    @server.tool(annotations=READ_ONLY)
    def read_session(
        session: Annotated[str, Field(description="session name from `status`")],
        lines: Annotated[
            int, Field(ge=1, le=2000, description="how many of the last lines to return")
        ] = 200,
    ) -> dict:
        """Recent text of the session's Claude Code screen (scrollback included) and
        Claude Code's state. Does not start the VM. Call it when the user asks for
        progress or the result; never poll it to wait for a BUSY Claude Code."""
        with tool_call("read_session"):
            target = f"session {session}"
            elapsed = busy_reads.seconds_since(target)
            if elapsed is not None:
                return {
                    "session": session,
                    "claude_state": BUSY,
                    **busy_reads.not_reread(elapsed),
                }
            guards.require_running(cfg)
            screen = connect.read_session(cfg, session, lines)
            busy = screen.get("claude_state") == BUSY
            busy_reads.record(target, busy)
            return {**screen, "note": BUSY_NOTE} if busy else screen

    @server.tool(annotations=STOPS)
    def stop() -> dict:
        """Stop the VM now (its disk is kept) and return without waiting. Interrupts
        any running work; the VM also stops by itself once every session is idle."""
        with write_call("stop"):
            return {"vm": gce.stop(cfg, wait=False)}

    return server
