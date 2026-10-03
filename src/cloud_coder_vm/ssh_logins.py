"""Interactive SSH logins outside tmux, as a third safety valve for the idle check.

Logins come from utmp (what `who` shows): sshd records a login only when it
allocates a terminal, so `gcloud compute ssh --command ...` calls (cloud-coder's
own install / launch / status) never appear. Each tmux pane also has a utmp entry
with host "tmux(<pid>).%N"; those are left to the tmux checks.
"""

import os
import struct
from dataclasses import dataclass
from pathlib import Path

from cloud_coder_vm.process_table import Process, is_shell

UTMP_PATH = Path("/var/run/utmp")
# struct utmp on Linux x86_64 / aarch64 (glibc), 384 bytes
_UTMP = struct.Struct("<hxxi32s4s32s256shhiii4i20s")
USER_PROCESS = 7


@dataclass(frozen=True)
class Login:
    user: str
    line: str  # e.g. "pts/0"
    host: str
    pid: int  # sshd process that owns the login
    tty: int  # device number of /dev/<line>; 0 if gone
    last_input: float  # atime of the terminal device, like `w`'s IDLE column


def parse_utmp(data: bytes) -> list[tuple[str, str, str, int]]:
    """(user, line, host, pid) of USER_PROCESS records."""
    records = []
    for offset in range(0, len(data) - _UTMP.size + 1, _UTMP.size):
        fields = _UTMP.unpack_from(data, offset)
        if fields[0] != USER_PROCESS:
            continue
        text = [
            f.split(b"\0", 1)[0].decode(errors="replace") for f in (fields[4], fields[2], fields[5])
        ]
        records.append((text[0], text[1], text[2], fields[1]))
    return records


def read_logins(utmp: Path = UTMP_PATH) -> list[Login]:
    try:
        records = parse_utmp(utmp.read_bytes())
    except OSError:
        return []
    logins = []
    for user, line, host, pid in records:
        try:
            st = os.stat(f"/dev/{line}")
            tty, last_input = st.st_rdev, st.st_atime
        except OSError:
            tty, last_input = 0, 0.0
        logins.append(Login(user, line, host, pid, tty, last_input))
    return logins


def busy_reasons(
    logins: list[Login],
    procs: dict[int, Process],
    now: float,
    idle_limit_seconds: float,
) -> list[str]:
    reasons = []
    for login in logins:
        if login.host.startswith("tmux("):
            continue  # a tmux pane: judged by the pane checks
        if login.pid not in procs or not login.tty:
            continue  # stale utmp record (sshd gone without cleaning up)
        on_tty = [p for p in procs.values() if p.tty == login.tty]
        if any(os.path.basename(p.argv0) == "tmux" for p in on_tty):
            continue  # attached to tmux: the panes decide, not the attach itself
        running = sorted({os.path.basename(p.argv0) for p in on_tty if not is_shell(p)})
        if running:
            reasons.append(f"ssh login {login.user} {login.line}: running {', '.join(running)}")
            continue
        idle = now - login.last_input
        if idle >= idle_limit_seconds:
            continue  # shell prompt with no input for long: a forgotten or dead terminal
        reasons.append(
            f"ssh login {login.user} {login.line} from {login.host or 'local'}"
            f" (no input for {idle / 60:.0f} min)"
        )
    return reasons
