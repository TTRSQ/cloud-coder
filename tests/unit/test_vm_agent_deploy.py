import subprocess
import sys

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
    assert "test -d ~/git/dotClaude && echo dotfiles-cloned" in check_command(cfg)
    assert "dotfiles" not in check_command(Config())
    assert str(paths.MANAGED_SETTINGS_FILE) in check_command(Config())
    out = f"aaa  {paths.AGENT_PYZ}\nbbb  {paths.CONFIG_PATH}\nclaude-installed\nhooks-installed\n"
    assert is_current(out, "aaa", "bbb")
    assert not is_current(out, "aaa", "bbb", dotfiles=True)
    assert is_current(out + "dotfiles-cloned\n", "aaa", "bbb", dotfiles=True)
