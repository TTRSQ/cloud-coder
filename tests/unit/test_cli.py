import json
import subprocess

import pytest

from cloud_coder import cli, connect
from cloud_coder.config import ConfigError


@pytest.fixture
def no_subprocess(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {args}")

    monkeypatch.setattr(subprocess, "run", fail)


def write_config(tmp_path, body: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(body)
    return str(path)


@pytest.mark.parametrize(
    ("body", "flags"),
    [
        ("gcp:\n  zone: asia-northeast1-b\n", []),
        ('gcp:\n  project: ""\n', []),
        ("", ["--project", ""]),
    ],
)
def test_missing_project_is_an_error_without_gcloud_fallback(tmp_path, no_subprocess, body, flags):
    config = write_config(tmp_path, body)
    args = cli.build_parser().parse_args(["status", "--config", config, *flags])
    with pytest.raises(ConfigError, match=r"set gcp\.project in .*config\.yaml or pass --project"):
        cli.resolve_config(args)


def test_project_flag_wins_over_config(tmp_path, no_subprocess):
    config = write_config(tmp_path, "gcp:\n  project: from-yaml\n")
    parser = cli.build_parser()
    assert cli.resolve_config(parser.parse_args(["status", "--config", config])).project == (
        "from-yaml"
    )
    args = parser.parse_args(["status", "--config", config, "--project", "from-flag"])
    assert cli.resolve_config(args).project == "from-flag"


def test_main_reports_target_on_stderr_and_keeps_json_stdout(tmp_path, monkeypatch, capsys):
    config = write_config(tmp_path, "gcp:\n  project: p1\n  zone: z1\n  instance: vm1\n")
    monkeypatch.setattr(connect, "status", lambda cfg: {"vm": "RUNNING"})
    assert cli.main(["status", "--json", "--config", config]) == 0
    out, err = capsys.readouterr()
    assert json.loads(out) == {"vm": "RUNNING"}
    assert "target project=p1 zone=z1 instance=vm1" in err


def test_main_without_project_fails(tmp_path, capsys, no_subprocess):
    config = write_config(tmp_path, "")
    assert cli.main(["stop", "--config", config]) == 1
    assert "no GCP project" in capsys.readouterr().err


def test_api_refuses_to_start_without_oauth_settings(tmp_path, no_subprocess, monkeypatch, capsys):
    from cloud_coder import http_api, oauth

    monkeypatch.setenv(oauth.PUBLIC_URL_ENV, "https://cc.example")
    monkeypatch.delenv(oauth.SIGNING_KEYS_ENV, raising=False)
    monkeypatch.setattr(http_api.uvicorn, "run", lambda *a, **kw: pytest.fail("served"))
    config = write_config(tmp_path, "gcp:\n  project: p\n")
    assert cli.main(["api", "--config", config]) == 1
    assert "no OAuth signing key" in capsys.readouterr().err
