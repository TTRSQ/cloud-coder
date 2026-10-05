import os
import subprocess
import sys
import time

import pytest

from cloud_coder import ssh, vm_agent_deploy
from cloud_coder.config import Config
from cloud_coder.vm_agent_deploy import build_pyz, is_current, sha256, vm_config
from cloud_coder_vm import paths
from cloud_coder_vm.system_files import parse_config, render_config


def test_pyz_is_reproducible_and_runs(tmp_path):
    data = build_pyz()
    assert sha256(data) == sha256(build_pyz())
    pyz = tmp_path / "a.pyz"
    pyz.write_bytes(data)
    out = subprocess.run([sys.executable, str(pyz), "--help"], capture_output=True, text=True)
    assert out.returncode == 0 and "idle-check" in out.stdout


def test_vm_config_roundtrip():
    vc = vm_config(Config(idle_grace_minutes=3, swap_gb=4))
    assert vc.grace_seconds == 180 and vc.swap_gb == 4 and vc.user == "coder"
    assert parse_config(render_config(vc)) == vc


def test_is_current():
    out = f"aaa  {paths.AGENT_PYZ}\nbbb  {paths.CONFIG_PATH}\nclaude-installed\nhooks-installed\n"
    assert is_current(out, "aaa", "bbb")
    assert not is_current(out, "zzz", "bbb")
    assert not is_current(out.replace("claude-installed", ""), "aaa", "bbb")
    assert not is_current("", "aaa", "bbb")


def test_check_command_and_dotfiles_marker():
    from cloud_coder.vm_agent_deploy import check_command

    cfg = Config(dotfiles_repo="https://github.com/TTRSQ/dotClaude.git")
    assert f"test -f ~/{paths.DOTFILES_STAMP} && echo dotfiles-installed" in check_command(cfg)
    assert "dotfiles" not in check_command(Config())
    assert str(paths.MANAGED_SETTINGS_FILE) in check_command(Config())
    out = f"aaa  {paths.AGENT_PYZ}\nbbb  {paths.CONFIG_PATH}\nclaude-installed\nhooks-installed\n"
    assert is_current(out, "aaa", "bbb")
    assert not is_current(out, "aaa", "bbb", dotfiles=True)
    assert is_current(out + "dotfiles-installed\n", "aaa", "bbb", dotfiles=True)


def run_on_fake_vm(home):
    """ssh.run that runs the command with a local shell in ``home``."""

    def run(cfg, command, **kw):
        env = {**os.environ, "HOME": str(home)}
        return subprocess.run(
            ["sh", "-c", command], capture_output=True, text=True, env=env, cwd=home
        )

    return run


@pytest.fixture
def fake_vm(monkeypatch, tmp_path):
    """The VM is a local shell in tmp_path; the install waits for tmp_path/gate."""
    monkeypatch.setattr(ssh, "run", run_on_fake_vm(tmp_path))
    monkeypatch.setattr(ssh, "scp", lambda *a: None)
    gate = tmp_path / "gate"
    outcome = {"install": "true"}
    monkeypatch.setattr(
        vm_agent_deploy,
        "install_command",
        lambda cfg, remote: (
            f"while [ ! -e {gate} ]; do sleep 0.05; done; {outcome['install']}; status=$?"
        ),
    )
    return gate, outcome


def wait_while(cfg, state):
    give_up_at = time.monotonic() + 5
    while vm_agent_deploy.install_state(cfg) == state:
        assert time.monotonic() < give_up_at


@pytest.mark.parametrize(
    ("install", "final"),
    [("true", vm_agent_deploy.INSTALLED), ("false", vm_agent_deploy.FAILED)],
)
def test_a_started_install_runs_detached_and_reports_its_end(
    monkeypatch, tmp_path, fake_vm, install, final
):
    gate, outcome = fake_vm
    outcome["install"] = install
    # an update replaces the agent and config early: the hashes match while it runs
    monkeypatch.setattr(vm_agent_deploy, "is_current", lambda *a, **kw: True)
    cfg = Config(project="p")
    started = time.monotonic()
    vm_agent_deploy.start_install(cfg)
    assert time.monotonic() - started < 2  # returns while the install still runs
    wait_while(cfg, vm_agent_deploy.INSTALLED)  # until the detached install holds the lock
    assert vm_agent_deploy.install_state(cfg) == vm_agent_deploy.INSTALLING
    gate.touch()
    wait_while(cfg, vm_agent_deploy.INSTALLING)
    assert vm_agent_deploy.install_state(cfg) == final


def test_a_failed_install_keeps_its_log_when_started_again(tmp_path, fake_vm):
    gate, outcome = fake_vm
    gate.touch()
    outcome["install"] = "echo first attempt; false"
    cfg = Config(project="p")
    vm_agent_deploy.start_install(cfg)
    wait_while(cfg, vm_agent_deploy.MISSING)
    wait_while(cfg, vm_agent_deploy.INSTALLING)
    assert vm_agent_deploy.install_state(cfg) == vm_agent_deploy.FAILED
    outcome["install"] = "true"
    vm_agent_deploy.start_install(cfg)
    assert "first attempt" in (tmp_path / f"{vm_agent_deploy.INSTALL_LOG}.prev").read_text()


def test_a_waiting_install_waits_for_one_running_on_the_vm(monkeypatch, tmp_path, fake_vm):
    gate, _ = fake_vm
    cfg = Config(project="p")
    vm_agent_deploy.start_install(cfg)
    wait_while(cfg, vm_agent_deploy.MISSING)
    monkeypatch.setattr(time, "sleep", lambda s: gate.touch())
    monkeypatch.setattr(vm_agent_deploy, "upload", lambda cfg: pytest.fail("installed twice"))
    monkeypatch.setattr(vm_agent_deploy, "is_current", lambda *a, **kw: True)
    assert vm_agent_deploy.ensure_installed(cfg) is False
