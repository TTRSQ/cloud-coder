"""Compute Engine lifecycle of the worker VM, done through the gcloud CLI."""

import json
import logging
import subprocess
import time
from dataclasses import dataclass

from cloud_coder import deadline
from cloud_coder.config import Config

ABSENT = "absent"
STOPPED = "stopped"
RUNNING = "running"
STARTING = "starting"
STOPPING = "stopping"
SUSPENDED = "suspended"

# https://cloud.google.com/compute/docs/instances/instance-life-cycle
_GCE_STATUS = {
    "PROVISIONING": STARTING,
    "STAGING": STARTING,
    "RUNNING": RUNNING,
    "STOPPING": STOPPING,
    "REPAIRING": STOPPING,
    "TERMINATED": STOPPED,
    "STOPPED": STOPPED,
    "SUSPENDING": STOPPING,
    "SUSPENDED": SUSPENDED,
}

LABEL = "cloud-coder"
NETWORK_TAG = "cloud-coder"

log = logging.getLogger(__name__)


class GcloudError(Exception):
    pass


@dataclass(frozen=True)
class Vm:
    status: str
    machine_type: str | None = None
    external_ip: str | None = None


def vm_from_describe(data: dict | None) -> Vm:
    if data is None:
        return Vm(ABSENT)
    status = _GCE_STATUS.get(data.get("status", ""), STARTING)
    machine_type = (data.get("machineType") or "").rsplit("/", 1)[-1] or None
    ip = None
    for nic in data.get("networkInterfaces", []):
        for ac in nic.get("accessConfigs", []):
            ip = ip or ac.get("natIP")
    return Vm(status, machine_type, ip)


def gcloud(cfg: Config, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    """Bounded by the current deadline (see deadline.py)."""
    cmd = ["gcloud", *args, f"--project={cfg.project}"]
    return deadline.run(cmd, capture_output=capture, text=True, stdin=subprocess.DEVNULL)


def _checked(cfg: Config, *args: str) -> str:
    result = gcloud(cfg, *args)
    if result.returncode != 0:
        raise GcloudError(f"gcloud {' '.join(args[:3])} failed:\n{result.stderr.strip()}")
    return result.stdout


def describe(cfg: Config) -> Vm:
    result = gcloud(
        cfg, "compute", "instances", "describe", cfg.instance, f"--zone={cfg.zone}", "--format=json"
    )
    if result.returncode != 0:
        if "was not found" in result.stderr or "notFound" in result.stderr:
            return Vm(ABSENT)
        raise GcloudError(f"gcloud compute instances describe failed:\n{result.stderr.strip()}")
    return vm_from_describe(json.loads(result.stdout))


def create_args(cfg: Config) -> list[str]:
    return [
        "compute",
        "instances",
        "create",
        cfg.instance,
        f"--zone={cfg.zone}",
        f"--machine-type={cfg.machine_type}",
        f"--image-family={cfg.image_family}",
        f"--image-project={cfg.image_project}",
        f"--boot-disk-size={cfg.disk_size_gb}GB",
        f"--boot-disk-type={cfg.disk_type}",
        f"--labels={LABEL}=worker",
        # Firewall rules for this VM (such as SSH from IAP) target this network tag.
        f"--tags={NETWORK_TAG}",
        # SSH keys are then added to this instance only, never to project metadata.
        "--metadata=block-project-ssh-keys=TRUE",
        # The worker needs no Google API access.
        "--no-service-account",
        "--no-scopes",
    ]


def wait_for(cfg: Config, wanted: set[str], timeout: float = 600) -> Vm:
    give_up_at = time.monotonic() + timeout
    while True:
        vm = describe(cfg)
        if vm.status in wanted:
            return vm
        if time.monotonic() > give_up_at:
            raise GcloudError(f"VM stayed {vm.status}; expected {sorted(wanted)}")
        time.sleep(5)


def ensure_running(cfg: Config, machine_type_requested: bool = False, wait: bool = True) -> str:
    """Create / start / resume the VM as needed. Returns the action taken.

    With ``wait=False`` the GCE operation is only requested (``--async``) and this returns
    at once; poll ``describe`` until the VM is running. A VM that is still stopping
    cannot be started yet: then the action is ``stopping`` and nothing is requested.
    """
    run_async = () if wait else ("--async",)
    vm = describe(cfg)
    if vm.status == STOPPING:
        if not wait:
            return STOPPING
        log.info("VM is stopping; waiting for it to stop")
        vm = wait_for(cfg, {STOPPED, SUSPENDED})
    if vm.status == ABSENT:
        log.info(
            f"creating VM {cfg.instance} ({cfg.machine_type}, {cfg.disk_size_gb}GB {cfg.disk_type})"
        )
        _checked(cfg, *create_args(cfg), *run_async)
        action = "created"
    elif vm.status == STOPPED:
        if machine_type_requested and vm.machine_type != cfg.machine_type:
            log.info(f"changing machine type {vm.machine_type} -> {cfg.machine_type}")
            _checked(
                cfg,
                "compute",
                "instances",
                "set-machine-type",
                cfg.instance,
                f"--zone={cfg.zone}",
                f"--machine-type={cfg.machine_type}",
            )
        log.info(f"starting VM {cfg.instance}")
        _checked(
            cfg, "compute", "instances", "start", cfg.instance, f"--zone={cfg.zone}", *run_async
        )
        action = "started"
    elif vm.status == SUSPENDED:
        log.info(f"resuming VM {cfg.instance}")
        _checked(
            cfg, "compute", "instances", "resume", cfg.instance, f"--zone={cfg.zone}", *run_async
        )
        action = "resumed"
    elif vm.status == STARTING:
        action = "started"
    else:
        if machine_type_requested and vm.machine_type != cfg.machine_type:
            log.info(
                f"VM is running as {vm.machine_type}; --machine-type applies after "
                "`cloud-coder stop`"
            )
        return "running"
    if wait:
        wait_for(cfg, {RUNNING})
    return action


def stop(cfg: Config, wait: bool = True) -> str:
    """Stop the VM. With ``wait=False`` the stop is only requested and this returns
    ``stopping``; poll ``describe`` until the VM is stopped."""
    vm = describe(cfg)
    if vm.status in (ABSENT, STOPPED):
        return vm.status
    run_async = () if wait else ("--async",)
    _checked(cfg, "compute", "instances", "stop", cfg.instance, f"--zone={cfg.zone}", *run_async)
    return STOPPED if wait else STOPPING
