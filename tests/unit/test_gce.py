from cloud_coder import gce
from cloud_coder.config import Config


def test_vm_from_describe():
    data = {
        "status": "TERMINATED",
        "machineType": "https://www.googleapis.com/compute/v1/projects/p/zones/z/machineTypes/e2-standard-8",
        "networkInterfaces": [{"accessConfigs": [{"natIP": "34.1.2.3"}]}],
    }
    vm = gce.vm_from_describe(data)
    assert vm == gce.Vm(gce.STOPPED, "e2-standard-8", "34.1.2.3")


def test_status_mapping():
    assert gce.vm_from_describe(None).status == gce.ABSENT
    for raw, want in [
        ("RUNNING", gce.RUNNING),
        ("STAGING", gce.STARTING),
        ("PROVISIONING", gce.STARTING),
        ("STOPPING", gce.STOPPING),
        ("STOPPED", gce.STOPPED),
        ("SUSPENDED", gce.SUSPENDED),
    ]:
        assert gce.vm_from_describe({"status": raw}).status == want


def test_create_args_use_config():
    args = gce.create_args(Config(machine_type="n2-standard-16", disk_size_gb=200))
    assert "--machine-type=n2-standard-16" in args
    assert "--boot-disk-size=200GB" in args
    assert "--boot-disk-type=pd-ssd" in args
    assert "--image-family=ubuntu-2404-lts-amd64" in args
    assert "--metadata=block-project-ssh-keys=TRUE" in args
    assert any(a.startswith("--labels=cloud-coder=") for a in args)


class FakeGcloud:
    """Stands in for gcloud: answers describe from ``statuses``, records other calls."""

    def __init__(self, monkeypatch, *statuses):
        self.statuses = list(statuses)
        self.calls = []
        monkeypatch.setattr(gce, "describe", self.describe)
        monkeypatch.setattr(gce, "_checked", lambda cfg, *args: self.calls.append(args) or "")
        monkeypatch.setattr(gce.time, "sleep", lambda s: None)

    def describe(self, cfg):
        status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        return gce.Vm(status)


def test_start_without_waiting_requests_async_and_returns(monkeypatch):
    fake = FakeGcloud(monkeypatch, gce.STOPPED)
    assert gce.ensure_running(Config(), wait=False) == "started"
    assert fake.calls == [
        ("compute", "instances", "start", "cloud-coder", "--zone=asia-northeast1-b", "--async")
    ]


def test_start_waits_until_running(monkeypatch):
    fake = FakeGcloud(monkeypatch, gce.STOPPED, gce.STARTING, gce.RUNNING)
    assert gce.ensure_running(Config()) == "started"
    assert "--async" not in fake.calls[0]
    assert fake.statuses == [gce.RUNNING]


def test_stopping_vm_is_not_started_without_waiting(monkeypatch):
    fake = FakeGcloud(monkeypatch, gce.STOPPING)
    assert gce.ensure_running(Config(), wait=False) == gce.STOPPING
    assert fake.calls == []


def test_stop_without_waiting(monkeypatch):
    fake = FakeGcloud(monkeypatch, gce.RUNNING)
    assert gce.stop(Config(), wait=False) == gce.STOPPING
    assert fake.calls[0][-1] == "--async"
    FakeGcloud(monkeypatch, gce.STOPPED)
    assert gce.stop(Config(), wait=False) == gce.STOPPED
