import base64
import io
import json
import shlex
import subprocess

import pytest

from cloud_coder import cli, connect, gce, ssh, vm_agent_deploy
from cloud_coder.config import Config
from cloud_coder.connect import launch_command


def test_launch_command_carries_prompt_as_base64():
    prompt = "fix it\n'quotes' $(rm -rf /) 日本語"
    argv = shlex.split(launch_command("git@github.com:o/r.git", None, False, False, prompt))
    assert argv[argv.index("--repo-url") + 1] == "git@github.com:o/r.git"
    assert base64.b64decode(argv[argv.index("--prompt-b64") + 1]).decode() == prompt


def test_launch_command_without_prompt():
    assert "--prompt-b64" not in launch_command("r", None, True, False)


def test_cli_prompt_sources(tmp_path, monkeypatch):
    parser = cli.build_parser()
    args = parser.parse_args(["connect", "r", "-p", "hello", "--detach"])
    assert cli.read_prompt(args) == "hello" and args.no_attach
    f = tmp_path / "p.txt"
    f.write_text("from file\nline 2")
    assert cli.read_prompt(parser.parse_args(["connect", "--prompt-file", str(f)])) == (
        "from file\nline 2"
    )
    monkeypatch.setattr("sys.stdin", io.StringIO("from stdin"))
    assert cli.read_prompt(parser.parse_args(["connect", "--prompt-file", "-"])) == "from stdin"
    assert cli.read_prompt(parser.parse_args(["connect"])) is None


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_agent_result_raises_on_agent_errors():
    assert connect.agent_result(completed('log line\n{"session": "cc-a-1"}\n')) == {
        "session": "cc-a-1"
    }
    with pytest.raises(connect.AgentError, match="unknown session"):
        connect.agent_result(completed('{"error": "unknown session \'x\'"}', returncode=1))
    with pytest.raises(connect.AgentError, match="Connection refused"):
        connect.agent_result(completed(stderr="ssh: Connection refused", returncode=255))


def test_up_without_waiting_is_not_ready_until_the_vm_runs(monkeypatch):
    monkeypatch.setattr(gce, "ensure_running", lambda cfg, mt, wait: "started")
    monkeypatch.setattr(ssh, "reachable", lambda cfg: pytest.fail("VM is not running"))
    assert connect.up(Config(), wait=False) == connect.UpResult("started", False, False)

    monkeypatch.setattr(gce, "ensure_running", lambda cfg, mt, wait: "running")
    monkeypatch.setattr(ssh, "reachable", lambda cfg: False)
    assert not connect.up(Config(), wait=False).ready

    monkeypatch.setattr(ssh, "reachable", lambda cfg: True)
    monkeypatch.setattr(vm_agent_deploy, "install_state", lambda cfg: vm_agent_deploy.INSTALLED)
    monkeypatch.setattr(vm_agent_deploy, "start_install", lambda cfg: pytest.fail("installed"))
    assert connect.up(Config(), wait=False) == connect.UpResult("running", True, False)


@pytest.fixture
def running_vm(monkeypatch):
    monkeypatch.setattr(gce, "ensure_running", lambda cfg, mt, wait: "running")
    monkeypatch.setattr(ssh, "reachable", lambda cfg: True)
    started = []
    monkeypatch.setattr(vm_agent_deploy, "start_install", lambda cfg: started.append(1))
    return started


def test_up_without_waiting_leaves_the_agent_install_running_on_the_vm(monkeypatch, running_vm):
    installing = connect.UpResult("running", False, False, agent_installing=True)
    for state, started in [(vm_agent_deploy.MISSING, 1), (vm_agent_deploy.INSTALLING, 1)]:
        monkeypatch.setattr(vm_agent_deploy, "install_state", lambda cfg, s=state: s)
        assert connect.up(Config(), wait=False) == installing
        assert len(running_vm) == started
    monkeypatch.setattr(vm_agent_deploy, "install_state", lambda cfg: vm_agent_deploy.INSTALLED)
    assert connect.up(Config(), wait=False) == connect.UpResult("running", True, False)


def test_a_failed_agent_install_is_reported_and_started_again(monkeypatch, running_vm):
    monkeypatch.setattr(vm_agent_deploy, "install_state", lambda cfg: vm_agent_deploy.FAILED)
    with pytest.raises(RuntimeError, match=r"installing the VM agent failed .*started it again"):
        connect.up(Config(), wait=False)
    assert running_vm == [1]


def test_up_with_waiting_installs_the_agent_before_returning(monkeypatch):
    monkeypatch.setattr(gce, "ensure_running", lambda cfg, mt, wait: "started")
    monkeypatch.setattr(ssh, "wait_ready", lambda cfg: None)
    monkeypatch.setattr(vm_agent_deploy, "ensure_installed", lambda cfg: True)
    assert connect.up(Config()) == connect.UpResult("started", True, True)


def test_cli_connect_detach_prints_the_session_and_does_not_attach(monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_config", lambda args: Config(project="p"))
    monkeypatch.setattr(connect, "connect", lambda *a, **kw: {"session": "cc-a-1"})
    monkeypatch.setattr(ssh, "attach_tmux", lambda *a: pytest.fail("attached"))
    assert cli.main(["connect", "a", "--detach"]) == 0
    assert json.loads(capsys.readouterr().out) == {"session": "cc-a-1"}


def test_cli_reports_agent_errors_on_stderr(monkeypatch, capsys):
    monkeypatch.setattr(cli, "resolve_config", lambda args: Config(project="p"))

    def fail(*a, **kw):
        raise connect.AgentError("Claude Code in this session is BUSY; prompt not sent")

    monkeypatch.setattr(connect, "connect", fail)
    assert cli.main(["connect", "a", "-p", "x", "--detach"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "error: Claude Code in this session is BUSY" in captured.err


def test_uncaptured_remote_output_goes_to_stderr_not_stdout(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.update(kw))
    ssh.run(Config(project="p"), "true", capture=False)
    assert seen["stdout"] == 2 and seen["stdin"] == subprocess.DEVNULL
