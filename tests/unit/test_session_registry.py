import pytest

from cloud_coder_vm.session_registry import (
    LogicalSession,
    latest,
    load,
    next_index,
    repo_name_from_url,
    save,
    session_name,
)


@pytest.mark.parametrize(
    "url,name",
    [
        ("git@github.com:owner/repo.git", "repo"),
        ("https://github.com/owner/repo", "repo"),
        ("https://github.com/owner/my.repo.git/", "my.repo"),
        ("/srv/git/repo.git", "repo"),
    ],
)
def test_repo_name_from_url(url, name):
    assert repo_name_from_url(url) == name


def test_session_name_is_tmux_safe():
    assert session_name("my.repo", 2) == "cc-my_repo-2"


def entry(name, repo, t):
    return LogicalSession(name, repo, None, f"/w/{name}", f"id-{name}", 0.0, t)


def test_next_index_and_latest():
    sessions = {
        s.name: s
        for s in [entry("cc-a-1", "a", 5), entry("cc-a-2", "a", 9), entry("cc-b-1", "b", 7)]
    }
    assert next_index(sessions, "a") == 3
    assert next_index(sessions, "c") == 1
    assert latest(sessions, "a").name == "cc-a-2"
    assert latest(sessions).name == "cc-a-2"
    assert latest(sessions, "zzz") is None


def test_roundtrip(tmp_path):
    path = tmp_path / "sub" / "sessions.json"
    sessions = {"cc-a-1": entry("cc-a-1", "a", 1)}
    save(path, sessions)
    assert load(path) == sessions
    assert load(tmp_path / "missing.json") == {}
