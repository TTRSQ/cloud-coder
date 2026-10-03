import subprocess

import pytest

from cloud_coder_vm import dev_tools
from cloud_coder_vm.dev_tools import DEFAULT_TOOLS, TOOLS, missing


def test_default_tools():
    assert DEFAULT_TOOLS == ("gh", "node", "rust", "docker", "uv")
    assert set(DEFAULT_TOOLS) <= set(TOOLS)


@pytest.mark.parametrize(
    "name,scope,url",
    [
        ("gh", "system", "https://cli.github.com/packages"),
        ("node", "system", "https://deb.nodesource.com/setup_lts.x"),
        ("docker", "system", "https://download.docker.com/linux/ubuntu"),
        ("rust", "user", "https://sh.rustup.rs"),
        ("uv", "user", "https://astral.sh/uv/install.sh"),
    ],
)
def test_official_sources(name, scope, url):
    assert TOOLS[name].scope == scope
    assert url in TOOLS[name].script


@pytest.mark.parametrize("name", list(TOOLS))
def test_scripts_are_valid_bash(name):
    subprocess.run(["bash", "-n", "-c", TOOLS[name].script], check=True)


def test_missing_skips_installed_user_tools(tmp_path):
    (tmp_path / ".local/bin").mkdir(parents=True)
    (tmp_path / ".local/bin/uv").write_text("")
    names = [t.name for t in missing(["gh", "rust", "uv"], "user", tmp_path)]
    assert names == ["rust"]
    assert missing([], "user", tmp_path) == []


def test_missing_system_tools_by_binary(monkeypatch):
    monkeypatch.setitem(TOOLS, "fake", dev_tools.Tool("fake", "system", "/bin/sh", "", ""))
    monkeypatch.setitem(TOOLS, "absent", dev_tools.Tool("absent", "system", "/nope/x", "", ""))
    assert [t.name for t in missing(["fake", "absent", "uv"], "system")] == ["absent"]
