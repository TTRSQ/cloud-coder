import pytest

from cloud_coder.config import (
    DEFAULT_MACHINE_TYPE,
    Config,
    ConfigError,
    from_mapping,
    load,
    with_overrides,
)


def test_defaults_match_recommended_spec():
    cfg = Config()
    assert cfg.machine_type == DEFAULT_MACHINE_TYPE
    assert cfg.disk_size_gb == 100
    assert cfg.disk_type == "pd-ssd"
    assert cfg.image_family == "ubuntu-2404-lts-amd64"
    assert cfg.auto_trust_workspace is True
    assert cfg.swap_gb == 0


def test_yaml_overrides(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(
        "gcp:\n  machine_type: n2-standard-16\n  disk_size_gb: 200\n  disk_type: pd-balanced\n"
        "ssh:\n  iap: true\nvm:\n  swap_gb: 4\nclaude:\n  auto_trust_workspace: false\n"
    )
    cfg = load(path)
    assert cfg.machine_type == "n2-standard-16"
    assert cfg.disk_size_gb == 200 and cfg.disk_type == "pd-balanced"
    assert cfg.iap is True and cfg.swap_gb == 4 and cfg.auto_trust_workspace is False


def test_missing_file_gives_defaults(tmp_path):
    assert load(tmp_path / "nope.yaml") == Config()


def test_cli_overrides_win_and_none_is_ignored():
    cfg = with_overrides(
        Config(machine_type="e2-standard-4"), machine_type="n2-standard-16", zone=None
    )
    assert cfg.machine_type == "n2-standard-16" and cfg.zone == Config().zone


@pytest.mark.parametrize(
    "data",
    [
        {"gcp": {"machine": "x"}},
        {"nope": {}},
        {"gcp": {"disk_size_gb": "100"}},
        {"ssh": {"iap": "yes"}},
        {"gcp": "x"},
    ],
)
def test_invalid_config_rejected(data):
    with pytest.raises(ConfigError):
        from_mapping(data)


def test_default_machine_type_is_t2d_standard_8():
    assert DEFAULT_MACHINE_TYPE == "t2d-standard-8"


def test_tools_config(tmp_path):
    assert Config().tools == ("gh", "node", "rust", "docker", "uv")
    assert from_mapping({"vm": {"tools": []}}).tools == ()
    assert from_mapping({"vm": {"tools": ["uv"], "ignore_docker": True}}).ignore_docker
    with pytest.raises(ConfigError):
        from_mapping({"vm": {"tools": ["emacs"]}})
    with pytest.raises(ConfigError):
        from_mapping({"vm": {"tools": "gh"}})


def test_ssh_and_git_config():
    cfg = Config()
    assert cfg.ignore_ssh_sessions is False and cfg.ssh_session_idle_minutes == 30
    assert cfg.github_https is True
    cfg = from_mapping(
        {
            "vm": {"ignore_ssh_sessions": True, "ssh_session_idle_minutes": 5},
            "git": {"github_https": False},
        }
    )
    assert cfg.ignore_ssh_sessions and cfg.ssh_session_idle_minutes == 5
    assert cfg.github_https is False


def test_layout_and_dotfiles_config():
    cfg = Config()
    assert (cfg.workspace, cfg.worktrees) == ("git", "git/wt")
    assert cfg.dotfiles_repo is None and cfg.dotfiles_install == "./install.sh"
    cfg = from_mapping(
        {
            "vm": {"workspace": "src", "worktrees": "src/wt"},
            "claude": {
                "dotfiles_repo": "https://github.com/TTRSQ/dotClaude.git",
                "dotfiles_branch": "main",
                "dotfiles_install": "./install.sh --dry-run",
            },
        }
    )
    assert cfg.worktrees == "src/wt" and cfg.dotfiles_branch == "main"
