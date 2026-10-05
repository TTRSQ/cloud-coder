import subprocess
import time

import pytest

from cloud_coder import deadline, gce, ssh
from cloud_coder.config import Config

CFG = Config(project="p")


def test_without_a_deadline_commands_are_not_bounded(monkeypatch):
    seen = {}
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.update(kw))
    ssh.run(CFG, "true")
    assert "timeout" not in seen


def test_commands_get_the_time_left(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kw: seen.append(kw["timeout"]))
    with deadline.within(40):
        gce.gcloud(CFG, "compute", "instances", "list")
        ssh.scp(CFG, "a", "b")
    assert len(seen) == 2 and all(0 < t <= 40 for t in seen)
    assert seen[1] <= seen[0]


def test_a_command_that_outlasts_the_deadline_is_a_timeout_error():
    started = time.monotonic()
    with deadline.within(0.2), pytest.raises(TimeoutError, match="did not finish in time"):
        deadline.run(["sleep", "5"])
    assert time.monotonic() - started < 3


def test_no_command_starts_after_the_deadline(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: pytest.fail("ran"))
    with deadline.within(0), pytest.raises(TimeoutError, match="out of time"):
        ssh.run(CFG, "true")
