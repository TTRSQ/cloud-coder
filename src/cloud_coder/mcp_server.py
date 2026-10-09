"""MCP server: the worker VM and its Claude Code sessions as tools for any MCP client.

The server is built for one target VM, fixed by the configuration it is created with;
no tool takes a project, zone or instance, and none runs arbitrary commands. Tools
return quickly: starting or stopping the VM is only requested, and the caller polls;
every call is bounded by a deadline. Claude Code's work takes far longer than an LLM
client's turn, so the instructions and results tell the client to hand control back to
the user once work has started, not to wait for it.
The server does not depend on a transport: `cloud-coder mcp` serves it over stdio, and
the HTTP API serves it at /mcp (Streamable HTTP) behind OAuth.
"""

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated

from mcp.server.auth.provider import TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from cloud_coder import config as config_mod
from cloud_coder import connect, deadline, gce, guards
from cloud_coder.config import Config
from cloud_coder.resource_usage import read as read_resource_usage
from cloud_coder_vm.session_state import BUSY

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Controls one cloud-coder worker: a GCE VM that runs Claude Code sessions in tmux and
stops itself when every session is idle.
Claude Code's work takes minutes to hours, far longer than one turn of yours. Once
`start_session` or `send_prompt` succeeds, tell the user the work has started and end
your turn. Do not poll `status` or `read_session` to wait for BUSY to end; call them only
when the user asks how the work is going or for its result. When `send_prompt` is
refused, do not resend it; tell the user.
- `status` shows the VM, its sessions and each Claude Code's state (BUSY / READY / IDLE).
- `up` starts the VM and returns at once. While `ready` is false the VM is starting
  (about a minute) or the agent is being installed (the first time, several minutes):
  call it again after a while, and if it is still not ready tell the user and stop.
- `start_session` with a `prompt` starts a new task: a new session (git worktree, tmux
  session) and a new Claude Code conversation for `repo`. To continue earlier work
  instead, pass its `session` (name from `status`); ask the user when it is unclear
  which they want. Without a prompt it opens the repository's latest session.
  The result's `conversation` says whether it is `new` or `continued`.
  `send_prompt` gives an existing session a new instruction. While Claude Code is BUSY
  it queues the instruction and takes it in at its next step, without stopping its
  work (`prompt` in the result is `queued`, else `sent`); it is refused while Claude
  Code waits for an answer to a permission prompt or a question. `read_session`
  returns the session's recent screen text: Claude Code's answer, its progress, or a
  question it is waiting on.
- `resource_usage` shows the VM's CPU, memory and disk usage now; it does not start
  the VM.
- `close_session` ends a session for good, even while Claude Code is BUSY; call it only
  when the user asks. When it is refused, tell the user what blocks it; do not retry.
- `stop` stops the VM at once, interrupting any work; normally let it stop itself.
"""

# The result fields that tell the client what to do next.
STARTED_NEXT = (
    "Claude Code is working on the prompt; this takes minutes to hours. Tell the user it "
    "has started and end your turn. Do not call status or read_session to wait for it; "
    "call them only when the user asks."
)
QUEUED_NEXT = (
    "Claude Code was working: it queued the instruction and takes it in at its next step, "
    "in the same conversation. Tell the user it was queued and end your turn. Do not call "
    "status or read_session to wait for it; call them only when the user asks."
)
SESSION_READY_NEXT = (
    "The session is ready and Claude Code has no new work. Tell the user; give it work "
    "with send_prompt when the user asks."
)
BUSY_NOTE = (
    "Claude Code is still working (BUSY). Report the progress so far to the user and end "
    "your turn; do not call status or read_session again in this turn."
)
PROMPT_NOT_RESENT = "Do not resend this prompt; tell the user and end your turn"

# A tool call ends within this. Clients cut tool calls off after a limit of their own
# (unpublished for ChatGPT): this keeps a stuck gcloud or ssh from outlasting it by far,
# while leaving room for the slow steps of a normal call (VM start, SSH to a new VM).
CALL_SECONDS = 120

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
STARTS = ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ACTS = ToolAnnotations(destructive_hint=False, open_world_hint=False)
ENDS = ToolAnnotations(destructive_hint=True, idempotent_hint=True, open_world_hint=False)

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
        if "; prompt not " in str(e):
            raise ToolError(f"{e}. {PROMPT_NOT_RESENT}") from e
        raise ToolError(str(e)) from e
    except EXPECTED_ERRORS as e:
        raise ToolError(str(e)) from e
    finally:
        log.info(f"MCP tool {name}: {outcome} in {time.monotonic() - started:.1f}s")


def build_server(
    cfg: Config, *, auth: AuthSettings | None = None, token_verifier: TokenVerifier | None = None
) -> MCPServer:
    """``auth`` and ``token_verifier`` protect the HTTP transports; a token that passes may
    call every tool. stdio has no tokens."""
    server = MCPServer(
        "cloud-coder", instructions=INSTRUCTIONS, auth=auth, token_verifier=token_verifier
    )

    def started(result: dict) -> dict:
        if result.get("prompt") == "queued":
            next_step = QUEUED_NEXT
        else:
            next_step = STARTED_NEXT if "prompt" in result else SESSION_READY_NEXT
        return {**result, "accepted": True, "next": next_step}

    @server.tool(annotations=READ_ONLY)
    def status() -> dict:
        """State of the VM and, when it runs, its sessions and auto-stop. Call it when the
        user asks; never poll it to wait for a BUSY Claude Code."""
        with tool_call("status"):
            st = connect.status(cfg)
            busy = any(s.get("claude_state") == BUSY for s in st.get("sessions", []))
            return {**st, "note": BUSY_NOTE} if busy else st

    @server.tool(annotations=STARTS)
    def up() -> dict:
        """Start the VM if it is not running and return without waiting. When it runs,
        also install or update the cloud-coder agent on it, in the background (the first
        install takes several minutes). Call again after a while until `ready` is true."""
        with tool_call("up"):
            return asdict(guards.up_now(cfg))

    @server.tool(annotations=ACTS)
    def start_session(
        repo: Annotated[
            str | None,
            Field(
                description="git URL to clone, or the name of a repository already on the "
                "VM. Needed with a prompt unless `session` is given; without either, the "
                "most recently used session is opened."
            ),
        ] = None,
        new: Annotated[
            bool,
            Field(
                description="start another session for the repo in a new git worktree even "
                "without a prompt (with a prompt and no `session` that is the default)"
            ),
        ] = False,
        session: Annotated[
            str | None,
            Field(
                description="continue this session and its Claude Code conversation (name "
                "from `status`); omit it to start a new task in a new session"
            ),
        ] = None,
        prompt: Annotated[
            str | None,
            Field(
                description="instruction for Claude Code. Without `session` it starts a "
                "new session and conversation; with `session` it continues that one (queued "
                "while Claude Code is BUSY)"
            ),
        ] = None,
    ) -> dict:
        """Make sure a session (git checkout, tmux session and Claude Code) exists and
        runs, and return its name. A prompt without `session` gets a new session and
        conversation; `conversation` in the result is `new` or `continued`. Requires a
        ready VM (see `up`). With a prompt, Claude Code works on it for minutes to hours:
        tell the user it has started and end your turn instead of waiting."""
        with tool_call("start_session"):
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
        """Type an instruction into the session's Claude Code and submit it. While Claude
        Code is BUSY it queues the instruction and takes it in at its next step (`prompt`
        is `queued`); otherwise `prompt` is `sent`. Refused while Claude Code waits for an
        answer to a permission prompt or a question (do not resend it then); if Claude Code
        is not running it is started (or resumed) with this instruction. Claude Code works
        on it for minutes to hours: tell the user and end your turn."""
        with tool_call("send_prompt"):
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
            guards.require_running(cfg)
            screen = connect.read_session(cfg, session, lines)
            busy = screen.get("claude_state") == BUSY
            return {**screen, "note": BUSY_NOTE} if busy else screen

    @server.tool(annotations=READ_ONLY)
    def resource_usage() -> dict:
        """The VM's CPU (utilization over 1 s, load average, cores), memory (used,
        available, total) and disk usage (per filesystem) now. Does not start the VM: when
        it is not running only its state is returned."""
        with tool_call("resource_usage"):
            return read_resource_usage(cfg)

    @server.tool(annotations=ENDS)
    def close_session(
        session: Annotated[str, Field(description="session name from `status`")],
    ) -> dict:
        """End the session for good: end its tmux session and Claude Code in it (even
        while BUSY), remove its git worktree and the branch when that is pushed, and
        remove it from `status`. The main checkout of a repository is kept. Refused, with
        nothing closed, while the worktree has uncommitted or unpushed work or ignored
        files other than regenerable caches: tell the user what blocks it. A session
        that is already closed is reported as unknown. Requires a ready VM (see `up`)."""
        with tool_call("close_session"):
            guards.require_ready(cfg)
            return connect.close_session(cfg, session)

    @server.tool(annotations=ENDS)
    def stop() -> dict:
        """Stop the VM now (its disk is kept) and return without waiting. Interrupts
        any running work; the VM also stops by itself once every session is idle."""
        with tool_call("stop"):
            return {"vm": gce.stop(cfg, wait=False)}

    return server
