"""Markers for cloud-coder's own long-running work (install, launch).

The idle check counts a marker as busy while its process is alive, so that work
can run without holding the state lock. A marker whose process is gone is stale
and removed.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

from cloud_coder_vm import paths


@contextmanager
def busy_marker(
    kind: str, directory: Path = paths.BUSY_DIR, required: bool = True
) -> Iterator[None]:
    """``required=False`` tolerates a missing runtime dir (the very first install)."""
    marker = directory / f"{kind}-{os.getpid()}"
    try:
        marker.write_text(f"{os.getpid()}\n")  # the directory comes from tmpfiles.d
    except OSError:
        if required:
            raise
    try:
        yield
    finally:
        marker.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def active(directory: Path = paths.BUSY_DIR) -> list[str]:
    """Kinds of work in progress; removes markers of processes that are gone."""
    kinds = []
    try:
        markers = sorted(directory.iterdir())
    except OSError:
        return kinds
    for marker in markers:
        kind, _, pid = marker.name.rpartition("-")
        if pid.isdigit() and _alive(int(pid)):
            kinds.append(kind)
        else:
            with suppress(OSError):
                marker.unlink()
    return kinds
