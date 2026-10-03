import struct

from cloud_coder_vm.process_table import Process
from cloud_coder_vm.ssh_logins import _UTMP, Login, busy_reasons, parse_utmp

NOW = 100_000.0
TTY = 34816
LIMIT = 30 * 60


def utmp_record(kind, pid, line, user, host):
    pad = [0, 0, 0, 0, 0, 0, 0, 0, 0, b""]
    return _UTMP.pack(kind, pid, line.encode(), b"ts/0", user.encode(), host.encode(), *pad)


def test_parse_utmp_keeps_user_processes_only():
    data = (
        utmp_record(2, 0, "~", "reboot", "6.8.0")  # BOOT_TIME
        + utmp_record(7, 1311, "pts/0", "coder", "123.100.158.6")
        + utmp_record(8, 0, "pts/2", "", "")  # DEAD_PROCESS
    )
    assert _UTMP.size == 384
    assert parse_utmp(data) == [("coder", "pts/0", "123.100.158.6", 1311)]
    assert parse_utmp(data[:-10]) == [("coder", "pts/0", "123.100.158.6", 1311)]
    assert struct.calcsize("<h") == 2


def login(host="1.2.3.4", pid=1311, idle=60.0, tty=TTY):
    return Login("coder", "pts/0", host, pid, tty, NOW - idle)


SSHD = Process(1311, 1, "sshd: coder [priv]")
SHELL = Process(1425, 1424, "-bash", tty=TTY)


def reasons(logins, procs):
    return busy_reasons(logins, {p.pid: p for p in procs}, NOW, LIMIT)


def test_active_ssh_shell_is_busy():
    assert "ssh login coder pts/0" in reasons([login()], [SSHD, SHELL])[0]


def test_ssh_shell_without_input_for_long_stops_counting():
    assert reasons([login(idle=LIMIT)], [SSHD, SHELL]) == []


def test_command_running_on_ssh_terminal_is_busy_even_without_input():
    make = Process(1500, 1425, "make", tty=TTY)
    assert "running make" in reasons([login(idle=10 * LIMIT)], [SSHD, SHELL, make])[0]


def test_tmux_attach_is_left_to_the_pane_checks():
    client = Process(1500, 1425, "tmux", tty=TTY)
    assert reasons([login()], [SSHD, SHELL, client]) == []


def test_tmux_pane_utmp_entries_are_ignored():
    assert reasons([login(host="tmux(1484).%0")], [SSHD, SHELL]) == []


def test_stale_utmp_record_is_ignored():
    assert reasons([login(pid=4242)], [SSHD, SHELL]) == []
    assert reasons([login(tty=0)], [SSHD]) == []
