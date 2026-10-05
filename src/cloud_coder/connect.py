"""Operations on the worker: `up`, `launch` / `connect`, `read_session` and `status`.

They return results and raise on failure; progress goes to the ``cloud_coder`` logger.
The command line and the MCP server are thin adapters over them.
"""

import base64
import json
import logging
import shlex
import subprocess
from dataclasses import dataclass

from cloud_coder import gce, ssh, vm_agent_deploy
from cloud_coder.config import Config
from cloud_coder_vm import paths

log = logging.getLogger(__name__)


class AgentError(RuntimeError):
    """The VM agent failed or reported an error."""


@dataclass(frozen=True)
class UpResult:
    vm_action: str  # created / started / resumed / running / stopping
    ready: bool  # VM running, SSH reachable and the agent installed
    agent_installed: bool  # this call installed the agent (only when it waits)
    agent_installing: bool = False  # an install runs on the VM; call `up` again later


def up(cfg: Config, machine_type_requested: bool = False, *, wait: bool = True) -> UpResult:
    """Make the VM ready for sessions. With ``wait=False`` a VM that is not running yet is
    only asked to start, a missing or outdated agent is installed by a process left
    running on the VM, and the result is not ready until a later call finds both done."""
    action = gce.ensure_running(cfg, machine_type_requested, wait=wait)
    log.info(f"VM {cfg.instance}: {action}")
    if wait:
        ssh.wait_ready(cfg)
        installed = vm_agent_deploy.ensure_installed(cfg)
        return UpResult(action, ready=True, agent_installed=installed)
    if action != gce.RUNNING or not ssh.reachable(cfg):
        return UpResult(action, ready=False, agent_installed=False)
    return _agent_ready_without_waiting(cfg, action)


def _agent_ready_without_waiting(cfg: Config, action: str) -> UpResult:
    """Start a missing or outdated agent install on the VM, and report whether one is
    done; the install runs there on its own."""
    state = vm_agent_deploy.install_state(cfg)
    if state == vm_agent_deploy.INSTALLED:
        return UpResult(action, ready=True, agent_installed=False)
    if state == vm_agent_deploy.FAILED:
        vm_agent_deploy.forget_failed_install(cfg)
        raise RuntimeError(
            "installing the VM agent failed; see ~/"
            f"{vm_agent_deploy.INSTALL_LOG} on the VM. The next `up` starts it again"
        )
    if state == vm_agent_deploy.MISSING:
        vm_agent_deploy.start_install(cfg)
    return UpResult(action, ready=False, agent_installed=False, agent_installing=True)


def is_repo_url(value: str) -> bool:
    return "/" in value or ":" in value


def launch_command(
    repo: str | None,
    session: str | None,
    new: bool,
    no_claude: bool,
    prompt: str | None = None,
) -> str:
    args = ["python3", str(paths.AGENT_PYZ), "launch"]
    if repo:
        args += ["--repo-url" if is_repo_url(repo) else "--repo", repo]
    if session:
        args += ["--session", session]
    if new:
        args.append("--new")
    if no_claude:
        args.append("--no-claude")
    if prompt is not None:
        # base64 keeps newlines and shell metacharacters intact through ssh
        args += ["--prompt-b64", base64.b64encode(prompt.encode()).decode()]
    return shlex.join(args)


def parse_agent_json(stdout: str) -> dict:
    lines = [line for line in stdout.splitlines() if line.startswith("{")]
    if not lines:
        raise AgentError(f"unexpected output from the VM agent: {stdout.strip()[-500:]}")
    return json.loads(lines[-1])


def agent_result(result: subprocess.CompletedProcess) -> dict:
    """The JSON a VM agent command printed; AgentError when it failed."""
    if result.returncode != 0 and not result.stdout.strip():
        raise AgentError(result.stderr.strip()[-500:] or f"exit status {result.returncode}")
    out = parse_agent_json(result.stdout)
    if "error" in out:
        raise AgentError(out["error"])
    return out


def launch(
    cfg: Config,
    repo: str | None,
    *,
    new: bool = False,
    session: str | None = None,
    no_claude: bool = False,
    prompt: str | None = None,
    forward_agent: bool = True,
) -> dict:
    """Ensure repo, tmux session and Claude Code on a ready VM, and hand Claude the prompt."""
    command = launch_command(repo, session, new, no_claude, prompt)
    return agent_result(ssh.run(cfg, command, forward_agent=forward_agent))


def connect(
    cfg: Config,
    repo: str | None,
    *,
    new: bool = False,
    session: str | None = None,
    no_claude: bool = False,
    prompt: str | None = None,
    machine_type_requested: bool = False,
) -> dict:
    """`up`, then `launch`. Returns what the VM agent did, including the session name."""
    up(cfg, machine_type_requested)
    launched = launch(cfg, repo, new=new, session=session, no_claude=no_claude, prompt=prompt)
    log.info(
        f"session {launched['session']} in {launched['workdir']}: repo {launched['repo']}, "
        f"tmux {launched['tmux']}, claude {launched.get('claude', 'not started')}"
        + (f", prompt {launched['prompt']}" if "prompt" in launched else "")
    )
    trust = launched.get("trust_written")
    if isinstance(trust, str):
        log.info(f"workspace trust {trust}; accept Claude Code's trust prompt in tmux")
    return launched


def read_session(cfg: Config, session: str, lines: int = 200) -> dict:
    """The last ``lines`` lines of the session's Claude Code pane, and its state."""
    command = shlex.join(
        [
            "python3",
            str(paths.AGENT_PYZ),
            "read-session",
            "--session",
            session,
            "--lines",
            str(lines),
        ]
    )
    return agent_result(ssh.run(cfg, command))


def status(cfg: Config) -> dict:
    vm = gce.describe(cfg)
    out: dict = {
        "instance": cfg.instance,
        "zone": cfg.zone,
        "vm": vm.status,
        "machine_type": vm.machine_type,
        "external_ip": vm.external_ip,
    }
    if vm.status != gce.RUNNING:
        return out
    result = ssh.run(cfg, shlex.join(["python3", str(paths.AGENT_PYZ), "status"]))
    if result.returncode != 0:
        out["agent"] = f"unavailable: {result.stderr.strip()[-300:]}"
        return out
    out.update(json.loads(result.stdout))
    return out


def format_status(st: dict) -> str:
    lines = [
        f"VM {st['instance']} ({st['zone']}): {st['vm']}"
        + (f", {st['machine_type']}" if st.get("machine_type") else "")
    ]
    if "agent" in st:
        lines.append(f"  agent: {st['agent']}")
    if "auto_stop" not in st:
        return "\n".join(lines)
    if st["auto_stop"] == "blocked":
        lines.append("  auto-stop: blocked")
        lines += [f"    - {reason}" for reason in st["busy_reasons"]]
    elif st.get("shutdown_in_seconds") is not None:
        lines.append(f"  auto-stop: idle, shutdown in {st['shutdown_in_seconds']:.0f}s")
    else:
        lines.append("  auto-stop: idle, grace period starts at the next check")
    lines.append(f"  idle-check timer: {st['idle_timer']}, grace {st['grace_seconds']}s")
    lines.append("  sessions:")
    for s in st["sessions"]:
        lines.append(
            f"    {s['name']}: tmux {s['tmux']}, claude {s['claude_state'] or '-'}, "
            f"{s['workdir']} (claude session {s['claude_session_id']})"
        )
    if not st["sessions"]:
        lines.append("    (none)")
    return "\n".join(lines)
