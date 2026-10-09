import subprocess

import pytest

from cloud_coder import gce, resource_usage, ssh
from cloud_coder.config import Config

CFG = Config(project="p")

# PROBE_COMMAND's output on a t2d-standard-8 worker (the second /proc/stat read is made up:
# 400 of 800 ticks busy, of which 100 are steal, with iowait counted as idle).
PROBE_OUTPUT = """\
cpu  8766045 1063775 377579 34336308 92854 0 50964 0 0 0
cpu  8766245 1063775 377679 34336658 92904 0 50964 100 50 0
cores 8
loadavg 0.00 0.05 0.06 1/400 665653
MemTotal:       32858372 kB
MemAvailable:   19447372 kB
SwapTotal:             0 kB
SwapFree:              0 kB
Filesystem         1-blocks        Used  Available Capacity Mounted on
/dev/root      102888095744 98457796608 4413521920      96% /
/dev/sda16        923156480   167235584  691277824      20% /boot
/dev/sdb       1073741824 0 1073741824 0% /mnt/disks/my data
"""


def test_parse_reports_cpu_memory_and_disks():
    usage = resource_usage.parse(PROBE_OUTPUT)
    assert usage["cpu"] == {
        "cores": 8,
        "utilization_percent": 50.0,
        "sample_seconds": 1,
        "load_average": [0.0, 0.05, 0.06],
    }
    assert usage["memory"] == {
        "total_gib": 31.34,
        "used_gib": 12.79,
        "available_gib": 18.55,
        "used_percent": 40.8,
        "swap_total_gib": 0.0,
        "swap_used_gib": 0.0,
    }
    assert usage["disks"][0] == {
        "mount": "/",
        "total_gib": 95.82,
        "used_gib": 91.7,
        "available_gib": 4.11,
        "used_percent": 95.7,
    }
    assert [d["mount"] for d in usage["disks"]] == ["/", "/boot", "/mnt/disks/my data"]
    assert usage["disks"][2]["used_percent"] == 0.0


def test_parse_names_what_could_not_be_read():
    partial = "\n".join(
        line for line in PROBE_OUTPUT.splitlines() if not line.startswith(("MemAvailable", "cores"))
    )
    with pytest.raises(resource_usage.ProbeError, match="could not read nproc, /proc/meminfo"):
        resource_usage.parse(partial)


def test_probe_reads_only_disk_backed_filesystems():
    command = resource_usage.PROBE_COMMAND
    assert "/proc/stat" in command and "/proc/meminfo" in command
    assert "-xtmpfs" in command and "-xoverlay" in command


def test_a_vm_that_is_not_running_is_reported_without_ssh(monkeypatch):
    monkeypatch.setattr(gce, "describe", lambda cfg: gce.Vm(gce.STOPPED, "t2d-standard-8"))
    monkeypatch.setattr(ssh, "run", lambda *a, **kw: pytest.fail("reached the VM"))
    assert resource_usage.read(CFG) == {
        "instance": "cloud-coder",
        "zone": "asia-northeast1-b",
        "vm": "stopped",
    }


def test_a_running_vm_is_probed_over_ssh(monkeypatch):
    commands = []

    def fake_run(cfg, command, **kw):
        commands.append(command)
        return subprocess.CompletedProcess([], 0, stdout=PROBE_OUTPUT, stderr="")

    monkeypatch.setattr(gce, "describe", lambda cfg: gce.Vm(gce.RUNNING, "t2d-standard-8"))
    monkeypatch.setattr(ssh, "run", fake_run)
    usage = resource_usage.read(CFG)
    assert commands == [resource_usage.PROBE_COMMAND]
    assert usage["vm"] == "running" and usage["machine_type"] == "t2d-standard-8"
    assert usage["cpu"]["cores"] == 8


def test_an_ssh_failure_carries_its_stderr(monkeypatch):
    monkeypatch.setattr(gce, "describe", lambda cfg: gce.Vm(gce.RUNNING))
    monkeypatch.setattr(
        ssh,
        "run",
        lambda cfg, command, **kw: subprocess.CompletedProcess(
            [], 255, stdout="", stderr="Connection timed out"
        ),
    )
    with pytest.raises(resource_usage.ProbeError, match="Connection timed out"):
        resource_usage.read(CFG)
