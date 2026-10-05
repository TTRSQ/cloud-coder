"""End-to-end check against a real GCP project. Opt in with ``pytest --run-e2e -m e2e``.

Needs gcloud credentials and these environment variables:
  CLOUD_CODER_E2E_PROJECT  GCP project to create the VM in
  CLOUD_CODER_E2E_ZONE     zone (default asia-northeast1-b)
  CLOUD_CODER_E2E_INSTANCE instance name (default cloud-coder-e2e)
  CLOUD_CODER_E2E_REPO     public git URL to clone (default this repository)

The VM is left stopped (not deleted) at the end. Claude Code is not logged in,
so this covers everything except real hook firing, Remote Control and resume
of a real conversation (see README "Manual verification").
"""

import json
import os
import subprocess
import time

import pytest

pytestmark = pytest.mark.e2e

PROJECT = os.environ.get("CLOUD_CODER_E2E_PROJECT")
ZONE = os.environ.get("CLOUD_CODER_E2E_ZONE", "asia-northeast1-b")
INSTANCE = os.environ.get("CLOUD_CODER_E2E_INSTANCE", "cloud-coder-e2e")
REPO = os.environ.get("CLOUD_CODER_E2E_REPO", "https://github.com/TTRSQ/cloud-coder.git")


@pytest.fixture(scope="module")
def cc(tmp_path_factory):
    if not PROJECT:
        pytest.skip("CLOUD_CODER_E2E_PROJECT is not set")
    config = tmp_path_factory.mktemp("cfg") / "config.yaml"
    config.write_text(
        f"gcp:\n  project: {PROJECT}\n  zone: {ZONE}\n  instance: {INSTANCE}\n"
        "vm:\n  idle_grace_minutes: 2\n"
    )

    def run(*args, check=True):
        result = subprocess.run(
            ["cloud-coder", *args, f"--config={config}"], capture_output=True, text=True
        )
        if check and result.returncode != 0:
            raise AssertionError(f"cloud-coder {args} failed:\n{result.stderr}")
        return result

    yield run
    run("stop", check=False)


def vm_shell(command: str) -> str:
    return subprocess.run(
        [
            "gcloud",
            "compute",
            "ssh",
            f"coder@{INSTANCE}",
            f"--zone={ZONE}",
            f"--project={PROJECT}",
            "--quiet",
            f"--command={command}",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def last_json(stdout: str) -> dict:
    return json.loads([line for line in stdout.splitlines() if line.startswith("{")][-1])


def test_connect_is_idempotent(cc):
    first = last_json(cc("connect", REPO, "--no-attach").stdout)
    again = last_json(cc("connect", REPO, "--no-attach").stdout)
    assert again["session"] == first["session"]
    assert again["repo"] == "existing" and again["tmux"] == "existing"
    assert again["claude"] == "running"
    pattern = f"remote-control {first['session']}$"
    claudes = vm_shell(f"pgrep -c -f '{pattern}' || true").strip()
    assert claudes == "1"


def test_new_session_uses_a_worktree(cc):
    first = last_json(cc("connect", REPO, "--no-attach").stdout)
    second = last_json(cc("connect", REPO, "--new", "--no-attach").stdout)
    assert second["session"] != first["session"]
    assert "/git/wt/" in second["workdir"]


def test_a_prompt_starts_a_new_session_unless_one_is_named(cc):
    latest = last_json(cc("connect", REPO, "--no-attach").stdout)
    task = last_json(cc("connect", REPO, "-p", "a new task", "--no-attach").stdout)
    assert task["created"] and task["session"] != latest["session"]
    assert task["conversation"] == "new" and "/git/wt/" in task["workdir"]
    # without a prompt, connect returns to the latest session: the one just created
    assert last_json(cc("connect", REPO, "--no-attach").stdout)["session"] == task["session"]
    # Claude Code is not logged in and never becomes READY, so a prompt for a running
    # one is refused: continue the session after its Claude Code is gone.
    claude = f"'remote-control {task['session']}( |$)'"  # the prompt follows the name
    vm_shell(f"pkill -f {claude}; for _ in $(seq 10); do pgrep -f {claude} || break; sleep 1; done")
    again = cc("connect", "--session", task["session"], "-p", "more", "--no-attach").stdout
    assert last_json(again)["session"] == task["session"] and not last_json(again)["created"]
    refused = cc("connect", "-p", "where?", "--no-attach", check=False)
    assert refused.returncode != 0 and "needs a repository" in refused.stderr


def test_close_removes_a_clean_worktree_session(cc):
    task = last_json(cc("connect", REPO, "-p", "to be closed", "--no-attach").stdout)
    closed = last_json(cc("close", task["session"]).stdout)
    assert closed["worktree"] == "removed" and closed["tmux"] == "killed"
    assert vm_shell(f"test -e {task['workdir']} && echo exists || echo gone").strip() == "gone"
    st = json.loads(cc("status", "--json").stdout)
    assert task["session"] not in {s["name"] for s in st["sessions"]}


def test_status_blocks_auto_stop_while_claude_has_not_reported(cc):
    st = json.loads(cc("status", "--json").stdout)
    assert st["vm"] == "running"
    assert st["auto_stop"] == "blocked"
    assert st["idle_timer"] == "active"


def test_idle_vm_stops_itself_and_keeps_the_workspace(cc):
    vm_shell("tmux kill-server || true; echo kept > ~/git/e2e-marker")
    deadline = time.monotonic() + 8 * 60
    while time.monotonic() < deadline:
        if json.loads(cc("status", "--json").stdout)["vm"] == "stopped":
            break
        time.sleep(20)
    else:
        pytest.fail("VM did not stop itself")
    cc("up")
    assert vm_shell("cat ~/git/e2e-marker").strip() == "kept"
    assert "cc-" in vm_shell("cat ~/.local/share/cloud-coder/sessions.json")
