import fcntl
import os
import time

from cloud_coder_vm.state_lock import state_lock


def test_lock_timeout_proceeds_without_lock(tmp_path):
    lock = tmp_path / "state.lock"
    fd = os.open(lock, os.O_RDONLY | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        with state_lock(lock, timeout=0.2) as held:
            assert held is False
        assert time.monotonic() - start < 2
    finally:
        os.close(fd)
    with state_lock(lock, timeout=0.2) as held:
        assert held is True
