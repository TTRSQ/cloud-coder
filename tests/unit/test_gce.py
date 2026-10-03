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
