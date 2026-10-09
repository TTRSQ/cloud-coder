"""CPU, memory and disk usage of the worker VM, read from /proc and df over SSH.

The probe is a fixed shell command that needs only the kernel and coreutils, not the VM
agent, tmux or Claude Code, so it works on any running VM whatever agent version it
has. It never starts the VM: a VM that is not running is reported as such.
"""

import shlex
import subprocess

from cloud_coder import gce, ssh
from cloud_coder.config import Config

# CPU utilization is measured over this many seconds between two reads of /proc/stat.
CPU_SAMPLE_SECONDS = 1
# Filesystems that are not on a disk: memory-backed (counted in memory) or images.
NOT_ON_DISK = ("tmpfs", "devtmpfs", "squashfs", "overlay", "efivarfs")

PROBE_COMMAND = " ; ".join(
    [
        "head -n1 /proc/stat",
        f"sleep {CPU_SAMPLE_SECONDS}",
        "head -n1 /proc/stat",
        'echo "cores $(nproc)"',
        'echo "loadavg $(cat /proc/loadavg)"',
        "grep -E '^(MemTotal|MemAvailable|SwapTotal|SwapFree):' /proc/meminfo",
        shlex.join(["df", "-P", "-B1", "-l", *(f"-x{t}" for t in NOT_ON_DISK)]),
    ]
)

GIB = 1024**3


class ProbeError(RuntimeError):
    """The VM's usage could not be read."""


def _gib(n: int) -> float:
    return round(n / GIB, 2)


def _percent(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _cpu_busy_and_total(line: str) -> tuple[int, int]:
    # proc_stat(5): user nice system idle iowait irq softirq steal [guest guest_nice].
    # guest and guest_nice are already counted in user and nice, so they are left out.
    ticks = [int(v) for v in line.split()[1:9]]
    idle = ticks[3] + ticks[4]
    total = sum(ticks)
    return total - idle, total


def parse(stdout: str) -> dict:
    """The usage that ``PROBE_COMMAND`` printed; ProbeError when a part is missing."""
    cpu_lines: list[str] = []
    fields: dict[str, list[str]] = {}
    meminfo: dict[str, int] = {}
    disks: list[dict] = []
    in_df = False
    for line in stdout.splitlines():
        if in_df:
            parts = line.split(maxsplit=5)
            if len(parts) == 6:
                _fs, size, used, available, _capacity, mount = parts
                size, used, available = int(size), int(used), int(available)
                disks.append(
                    {
                        "mount": mount,
                        "total_gib": _gib(size),
                        "used_gib": _gib(used),
                        "available_gib": _gib(available),
                        # As df's Use%: blocks reserved for root count as neither.
                        "used_percent": _percent(used, used + available),
                    }
                )
        elif line.startswith("cpu "):
            cpu_lines.append(line)
        elif line.startswith(("cores ", "loadavg ")):
            key, _, rest = line.partition(" ")
            fields[key] = rest.split()
        elif line.startswith("Filesystem"):
            in_df = True
        elif ":" in line:
            key, _, rest = line.partition(":")
            meminfo[key] = int(rest.split()[0]) * 1024

    missing = [
        name
        for name, ok in [
            ("/proc/stat", len(cpu_lines) == 2),
            ("nproc", "cores" in fields),
            ("/proc/loadavg", "loadavg" in fields),
            ("/proc/meminfo", {"MemTotal", "MemAvailable"} <= set(meminfo)),
            ("df", in_df),
        ]
        if not ok
    ]
    if missing:
        raise ProbeError(f"could not read {', '.join(missing)} on the VM")

    (busy0, total0), (busy1, total1) = (_cpu_busy_and_total(line) for line in cpu_lines)
    mem_total, mem_available = meminfo["MemTotal"], meminfo["MemAvailable"]
    swap_total = meminfo.get("SwapTotal", 0)
    swap_used = swap_total - meminfo.get("SwapFree", 0)
    return {
        "cpu": {
            "cores": int(fields["cores"][0]),
            "utilization_percent": _percent(busy1 - busy0, total1 - total0),
            "sample_seconds": CPU_SAMPLE_SECONDS,
            "load_average": [float(v) for v in fields["loadavg"][:3]],
        },
        "memory": {
            "total_gib": _gib(mem_total),
            "used_gib": _gib(mem_total - mem_available),
            "available_gib": _gib(mem_available),
            "used_percent": _percent(mem_total - mem_available, mem_total),
            "swap_total_gib": _gib(swap_total),
            "swap_used_gib": _gib(swap_used),
        },
        "disks": disks,
    }


def read(cfg: Config) -> dict:
    """The VM's CPU, memory and disk usage now; only the VM status when it is not running."""
    vm = gce.describe(cfg)
    out: dict = {"instance": cfg.instance, "zone": cfg.zone, "vm": vm.status}
    if vm.status != gce.RUNNING:
        return out
    out["machine_type"] = vm.machine_type
    result: subprocess.CompletedProcess = ssh.run(cfg, PROBE_COMMAND)
    try:
        return {**out, **parse(result.stdout)}
    except ProbeError as e:
        detail = result.stderr.strip()[-300:]
        raise ProbeError(f"{e}: {detail}" if detail else str(e)) from e
