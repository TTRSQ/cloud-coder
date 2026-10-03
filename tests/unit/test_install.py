import json
import os
import subprocess

from cloud_coder_vm import paths
from cloud_coder_vm.install import (
    HOOK_EVENTS,
    cloud_coder_hooks,
    install_dotfiles,
    remove_legacy_user_hooks,
    without_cloud_coder_hooks,
)
from cloud_coder_vm.system_files import VmConfig, render_managed_hooks

OURS = paths.HOOK_COMMAND


def our_handlers(hooks):
    return [
        (event, group.get("matcher"))
        for event, groups in hooks.items()
        for group in groups
        for h in group["hooks"]
        if h["command"] == OURS
    ]


def test_managed_settings_hold_only_our_hooks():
    data = json.loads(render_managed_hooks(cloud_coder_hooks()))
    assert list(data) == ["hooks"]  # nothing that could restrict the user's own settings
    assert sorted(our_handlers(data["hooks"])) == sorted(HOOK_EVENTS)
    assert data["hooks"]["Notification"][0]["matcher"] == "idle_prompt"


def test_paths_are_the_documented_linux_managed_settings_dir():
    assert str(paths.MANAGED_SETTINGS_FILE.parent) == "/etc/claude-code/managed-settings.d"
    assert paths.MANAGED_SETTINGS_FILE.suffix == ".json"


LEGACY = {
    "model": "opus",
    "hooks": {
        "Stop": [
            {"hooks": [{"type": "command", "command": OURS}, {"type": "command", "command": "x"}]}
        ],
        "SessionEnd": [{"hooks": [{"type": "command", "command": OURS, "timeout": 10}]}],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}],
    },
}


def test_without_cloud_coder_hooks_keeps_everything_else():
    cleaned = without_cloud_coder_hooks(LEGACY)
    assert cleaned == {
        "model": "opus",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "x"}]}],
            "PreToolUse": LEGACY["hooks"]["PreToolUse"],
        },
    }
    assert without_cloud_coder_hooks(cleaned) == cleaned
    only_ours = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": OURS}]}]}}
    assert without_cloud_coder_hooks(only_ours) == {}


def test_remove_legacy_user_hooks_regular_file(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps(LEGACY))
    os.chmod(settings, 0o600)
    assert remove_legacy_user_hooks(settings) == "removed"
    assert OURS not in settings.read_text()
    assert json.loads(settings.read_text())["model"] == "opus"
    assert os.stat(settings).st_mode & 0o777 == 0o600
    assert remove_legacy_user_hooks(settings) == "clean"
    assert remove_legacy_user_hooks(tmp_path / "missing.json") == "clean"


def test_remove_legacy_user_hooks_never_writes_through_a_symlink(tmp_path):
    target = tmp_path / "dotfiles" / "settings.json"
    target.parent.mkdir()
    target.write_text(json.dumps(LEGACY))
    link = tmp_path / "settings.json"
    link.symlink_to(target)
    assert remove_legacy_user_hooks(link) == "linked"
    assert json.loads(target.read_text()) == LEGACY
    assert link.is_symlink()


def vm_config(**kw):
    base = dict(
        user="coder",
        grace_seconds=600,
        workspace="git",
        worktrees="git/wt",
        auto_trust_workspace=True,
        swap_gb=0,
        tools=[],
        ignore_docker=False,
        ignore_ssh_sessions=False,
        ssh_session_idle_minutes=30,
        github_https=True,
        dotfiles_repo=None,
        dotfiles_branch=None,
        dotfiles_install="./install.sh",
    )
    base.update(kw)
    return VmConfig(**base)


def make_dotfiles_repo(tmp_path):
    src = tmp_path / "src" / "dotClaude"
    src.mkdir(parents=True)
    (src / "install.sh").write_text('#!/bin/bash\necho run >> "$HOME/runs"\n')
    os.chmod(src / "install.sh", 0o755)
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["add", "."],
        ["-c", "user.email=a@b", "-c", "user.name=a", "commit", "-qm", "init"],
    ):
        subprocess.run(["git", *cmd], cwd=src, check=True)
    return src


def test_install_dotfiles_clones_once_and_reruns_install(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "git").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    src = make_dotfiles_repo(tmp_path)
    config = vm_config(dotfiles_repo=str(src))
    assert install_dotfiles(config, home) == "installed"
    clone = home / "git" / "dotClaude"
    (clone / "local-edit").write_text("keep")
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=a@b",
            "-c",
            "user.name=a",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "upstream",
        ],
        cwd=src,
        check=True,
    )
    assert install_dotfiles(config, home) == "installed"
    assert (clone / "local-edit").read_text() == "keep"  # never pulled or reset
    upstream = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True
    ).stdout
    local = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True
    ).stdout
    assert local != upstream
    assert (home / "runs").read_text() == "run\nrun\n"


def test_install_dotfiles_clone_failure_is_reported_not_fatal(tmp_path):
    config = vm_config(dotfiles_repo=str(tmp_path / "nope.git"))
    (tmp_path / "git").mkdir()
    assert install_dotfiles(config, tmp_path) == "clone failed"
    assert install_dotfiles(vm_config(), tmp_path) == "not configured"


def test_git_url_rewrite_is_idempotent_and_keeps_other_values():
    from cloud_coder_vm.install import GITHUB_SSH_PREFIXES, git_url_rewrite_changes

    assert git_url_rewrite_changes([], True) == (list(GITHUB_SSH_PREFIXES), [])
    assert git_url_rewrite_changes(list(GITHUB_SSH_PREFIXES) + ["gh:"], True) == ([], [])
    assert git_url_rewrite_changes(["git@github.com:", "gh:"], False) == ([], ["git@github.com:"])
    assert git_url_rewrite_changes(["gh:"], False) == ([], [])


def test_configure_github_https_with_real_git(tmp_path, monkeypatch):
    import subprocess

    from cloud_coder_vm.install import configure_github_https

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / ".gitconfig"))
    helper = "!/usr/bin/gh auth git-credential"
    subprocess.run(
        ["git", "config", "--global", "credential.https://github.com.helper", helper], check=True
    )

    def values():
        out = subprocess.run(
            ["git", "config", "--global", "--get-all", "url.https://github.com/.insteadOf"],
            capture_output=True,
            text=True,
        )
        return out.stdout.split()

    configure_github_https(True)
    configure_github_https(True)
    assert values() == ["git@github.com:", "ssh://git@github.com/"]
    rewritten = subprocess.run(
        ["git", "ls-remote", "--get-url", "git@github.com:o/r.git"],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    ).stdout.strip()
    assert rewritten == "https://github.com/o/r.git"
    configure_github_https(False)
    assert values() == []
    helper_now = subprocess.run(
        ["git", "config", "--global", "credential.https://github.com.helper"],
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert helper_now == helper
