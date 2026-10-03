import json
import os

import pytest

from cloud_coder_vm.workspace_trust import trust, with_trusted


def test_with_trusted_keeps_existing_project_keys():
    config = {"numStartups": 3, "projects": {"/w/a": {"allowedTools": ["Bash"], "x": 1}}}
    out = with_trusted(config, ["/w/a", "/w/b"])
    assert out["numStartups"] == 3
    assert out["projects"]["/w/a"] == {
        "allowedTools": ["Bash"],
        "x": 1,
        "hasTrustDialogAccepted": True,
    }
    assert out["projects"]["/w/b"] == {"hasTrustDialogAccepted": True}
    assert "hasTrustDialogAccepted" not in config["projects"]["/w/a"]  # input untouched


def test_with_trusted_returns_none_when_nothing_to_do():
    config = {"projects": {"/w/a": {"hasTrustDialogAccepted": True}}}
    assert with_trusted(config, ["/w/a"]) is None


def test_trust_writes_atomically_and_preserves_mode(tmp_path):
    ws = tmp_path / "workspace"
    repo = ws / "repo"
    repo.mkdir(parents=True)
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({"userID": "u", "projects": {"/elsewhere": {"a": 1}}}))
    os.chmod(cfg, 0o600)
    assert trust(cfg, [repo], ws) is True
    data = json.loads(cfg.read_text())
    assert data["userID"] == "u" and data["projects"]["/elsewhere"] == {"a": 1}
    assert data["projects"][str(repo.resolve())]["hasTrustDialogAccepted"] is True
    assert os.stat(cfg).st_mode & 0o777 == 0o600
    assert trust(cfg, [repo], ws) is False  # idempotent


def test_trust_creates_missing_config(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "r").mkdir(parents=True)
    cfg = tmp_path / ".claude.json"
    assert trust(cfg, [ws / "r"], ws)
    assert os.stat(cfg).st_mode & 0o777 == 0o600


@pytest.mark.parametrize("target", ["workspace", "home", "outside"])
def test_trust_refuses_outside_workspace(tmp_path, target):
    ws = tmp_path / "workspace"
    ws.mkdir()
    (tmp_path / "outside").mkdir()
    path = {"workspace": ws, "home": tmp_path, "outside": tmp_path / "outside"}[target]
    with pytest.raises(ValueError):
        trust(tmp_path / ".claude.json", [path], ws)
    assert not (tmp_path / ".claude.json").exists()


def test_trust_follows_symlinked_config(tmp_path):
    ws = tmp_path / "workspace"
    (ws / "r").mkdir(parents=True)
    real = tmp_path / "dotfiles" / "claude.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"userID": "u"}))
    link = tmp_path / ".claude.json"
    link.symlink_to(real)
    assert trust(link, [ws / "r"], ws)
    assert link.is_symlink()
    assert json.loads(real.read_text())["projects"][str((ws / "r").resolve())] == {
        "hasTrustDialogAccepted": True
    }
