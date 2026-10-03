from cloud_coder_vm import paths
from cloud_coder_vm.install import HOOK_EVENTS, merge_hooks

OURS = paths.HOOK_COMMAND


def our_handlers(settings):
    found = []
    for event, groups in settings["hooks"].items():
        for group in groups:
            for h in group["hooks"]:
                if h["command"] == OURS:
                    found.append((event, group.get("matcher")))
    return found


def test_merge_into_empty():
    merged = merge_hooks({})
    assert sorted(our_handlers(merged)) == sorted(HOOK_EVENTS)
    note = merged["hooks"]["Notification"][0]
    assert note["matcher"] == "idle_prompt"


def test_merge_is_idempotent_and_keeps_user_hooks():
    user = {
        "model": "opus",
        "hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": "say done"}]}],
            "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "x"}]}],
        },
    }
    once = merge_hooks(user)
    twice = merge_hooks(once)
    assert once == twice
    assert once["model"] == "opus"
    assert once["hooks"]["PreToolUse"] == user["hooks"]["PreToolUse"]
    assert {"type": "command", "command": "say done"} in once["hooks"]["Stop"][0]["hooks"]
    assert len(our_handlers(once)) == len(HOOK_EVENTS)
    assert user["hooks"]["Stop"] == [{"hooks": [{"type": "command", "command": "say done"}]}]


def test_merge_separates_our_handler_from_shared_group():
    shared = {
        "hooks": {
            "Stop": [
                {
                    "hooks": [
                        {"type": "command", "command": OURS},
                        {"type": "command", "command": "other"},
                    ]
                }
            ]
        }
    }
    merged = merge_hooks(shared)
    assert len(our_handlers(merged)) == len(HOOK_EVENTS)
    assert {"type": "command", "command": "other"} in merged["hooks"]["Stop"][0]["hooks"]


def test_install_in_progress(tmp_path):
    import os

    from cloud_coder_vm.install import install_in_progress

    marker = tmp_path / "installing"
    assert not install_in_progress(marker)
    marker.write_text(f"{os.getpid()}\n")
    assert install_in_progress(marker)
    marker.write_text("999999999\n")
    assert not install_in_progress(marker)


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
