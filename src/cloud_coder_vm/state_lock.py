"""VM-wide lock that serialises shutdown decisions against new work."""

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from cloud_coder_vm import paths


@contextmanager
def state_lock(lock_path: Path = paths.STATE_LOCK, timeout: float | None = None) -> Iterator[bool]:
    """Hold the lock. With ``timeout``, give up after that many seconds and yield False
    (the caller then proceeds without the lock); otherwise wait and yield True.

    Holders keep it only for short, local file updates; slow work uses busy markers.
    """
    fd = os.open(lock_path, os.O_RDONLY | os.O_CREAT, 0o644)
    try:
        if timeout is None:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield True
            return
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                yield True
                return
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.05)
        yield False
    finally:
        os.close(fd)


def write_atomic(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)
