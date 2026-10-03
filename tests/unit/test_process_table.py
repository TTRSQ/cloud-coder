from cloud_coder_vm.process_table import Process, is_claude, is_shell, read_process


def write_proc(root, pid, stat, cmdline):
    d = root / str(pid)
    d.mkdir()
    (d / "stat").write_text(stat)
    (d / "cmdline").write_bytes(b"\0".join(cmdline) + b"\0")


def test_read_process_handles_parentheses_in_comm(tmp_path):
    fields = " ".join(["S", "42"] + ["0"] * 17 + ["777"] + ["0"] * 10)
    write_proc(tmp_path, 100, f"100 (we(i)rd name) {fields}", [b"/usr/bin/python3", b"x"])
    proc = read_process(100, tmp_path)
    assert proc.ppid == 42 and proc.starttime == 777 and proc.argv0 == "/usr/bin/python3"


def test_read_process_sees_claude_under_node(tmp_path):
    fields = " ".join(["S", "1"] + ["0"] * 28)
    cli = b"/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"
    write_proc(tmp_path, 7, f"7 (node) {fields}", [b"node", cli])
    assert is_claude(read_process(7, tmp_path))


def test_identification():
    assert is_claude(Process(1, 0, "claude"))
    assert is_claude(Process(1, 0, "2.1.300", "/home/u/.local/share/claude/versions/2.1.300"))
    assert not is_claude(Process(1, 0, "python3", "/usr/bin/python3.12"))
    assert is_shell(Process(1, 0, "-bash")) and is_shell(Process(1, 0, "/usr/bin/zsh"))
    assert not is_shell(Process(1, 0, "vim"))
