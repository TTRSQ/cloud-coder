"""`up`, `connect` and `status` flows: VM -> SSH -> agent -> repo/tmux/Claude -> attach."""

import base64
import json
import shlex
import sys

from cloud_coder import gce, ssh, vm_agent_deploy
from cloud_coder.config import Config
from cloud_coder_vm import paths


def _log(message: str) -> None:
    print(f"cloud-coder: {message}", file=sys.stderr, flush=True)


def up(cfg: Config, machine_type_requested: bool = False) -> None:
    action = gce.ensure_running(cfg, machine_type_requested)
    _log(f"VM {cfg.instance}: {action}")
    ssh.wait_ready(cfg)
    vm_agent_deploy.ensure_installed(cfg)


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
        raise RuntimeError(f"unexpected output from the VM agent: {stdout.strip()[-500:]}")
    return json.loads(lines[-1])


def connect(
    cfg: Config,
    repo: str | None,
    *,
    new: bool = False,
    session: str | None = None,
    attach: bool = True,
    no_claude: bool = False,
    prompt: str | None = None,
    machine_type_requested: bool = False,
) -> int:
    up(cfg, machine_type_requested)
    command = launch_command(repo, session, new, no_claude, prompt)
    result = ssh.run(cfg, command, forward_agent=True)
    if result.returncode != 0 and not result.stdout.strip():
        _log(result.stderr.strip())
        return 1
    launched = parse_agent_json(result.stdout)
    if "error" in launched:
        _log(f"error: {launched['error']}")
        return 1
    _log(
        f"session {launched['session']} in {launched['workdir']}: repo {launched['repo']}, "
        f"tmux {launched['tmux']}, claude {launched.get('claude', 'not started')}"
        + (f", prompt {launched['prompt']}" if "prompt" in launched else "")
    )
    print(json.dumps(launched))
    if not attach:
        return 0
    return ssh.attach_tmux(cfg, launched["session"])


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
