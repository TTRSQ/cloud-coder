from cloud_coder_vm.idle_check import READY_IDLE_AFTER_SECONDS, decide
from cloud_coder_vm.process_table import Process
from cloud_coder_vm.session_state import BUSY, IDLE, READY, SessionState
from cloud_coder_vm.tmux_panes import Pane

CLAUDE_EXE = "/home/coder/.local/share/claude/versions/2.1.300"


def procs(*items):
    return {p.pid: p for p in items}


def shell(pid, ppid=1):
    return Process(pid, ppid, "-bash", "/usr/bin/bash", 100)


def claude(pid, ppid, start=500):
    return Process(pid, ppid, "claude", CLAUDE_EXE, start)


def other(pid, ppid, name):
    return Process(pid, ppid, name, f"/usr/bin/{name}", 100)


def state(key, s, pane, pid=None, start=500, session="cc-repo-1"):
    return SessionState(
        key=key,
        session_id="sid-" + key,
        state=s,
        tmux_pane=pane,
        cloud_coder_session=session,
        claude_pid=pid,
        claude_starttime=start if pid else None,
    )


NOW = 10_000.0


def evaluate(panes, procs_, states, now=NOW):
    from cloud_coder_vm.idle_check import evaluate as real

    for st in states:
        if not st.updated_at:
            st.updated_at = now
    return real(panes, procs_, states, now)


PANE1 = Pane("cc-repo-1", "%1", 10, "bash")
PANE2 = Pane("cc-repo-2", "%2", 20, "bash")


def test_no_tmux_and_no_sessions_is_idle():
    ev = evaluate([], {}, [])
    assert ev.idle and ev.busy_reasons == []


def test_tmux_query_failure_is_busy():
    assert not evaluate(None, {}, []).idle


def test_shell_at_prompt_is_idle():
    assert evaluate([PANE1], procs(shell(10)), []).idle


def test_foreground_command_in_shell_is_busy():
    ev = evaluate([PANE1], procs(shell(10), other(11, 10, "pytest")), [])
    assert not ev.idle
    assert "pytest" in ev.busy_reasons[0]


def test_background_job_of_shell_is_busy():
    ev = evaluate([PANE1], procs(shell(10), other(11, 10, "sleep")), [])
    assert not ev.idle


def test_pane_running_non_shell_is_busy():
    pane = Pane("s", "%3", 30, "htop")
    assert not evaluate([pane], procs(other(30, 1, "htop")), []).idle


def test_idle_claude_is_idle_and_its_children_are_ignored():
    p = procs(shell(10), claude(11, 10), other(12, 11, "node"))  # e.g. an MCP server
    ev = evaluate([PANE1], p, [state("pane-1", IDLE, "%1", pid=11)])
    assert ev.idle, ev.busy_reasons


def test_busy_or_ready_claude_blocks():
    p = procs(shell(10), claude(11, 10))
    for s in (BUSY, READY):
        ev = evaluate([PANE1], p, [state("pane-1", s, "%1", pid=11)])
        assert not ev.idle
        assert s in ev.busy_reasons[0]


def test_one_busy_claude_among_many_blocks():
    p = procs(shell(10), claude(11, 10), shell(20), claude(21, 20, start=600))
    states = [
        state("pane-1", IDLE, "%1", pid=11),
        state("pane-2", BUSY, "%2", pid=21, start=600, session="cc-repo-2"),
    ]
    ev = evaluate([PANE1, PANE2], p, states)
    assert not ev.idle
    assert len(ev.busy_reasons) == 1 and "cc-repo-2" in ev.busy_reasons[0]


def test_all_claudes_idle_is_idle():
    p = procs(shell(10), claude(11, 10), shell(20), claude(21, 20, start=600))
    states = [
        state("pane-1", IDLE, "%1", pid=11),
        state("pane-2", IDLE, "%2", pid=21, start=600),
    ]
    assert evaluate([PANE1, PANE2], p, states).idle


def test_claude_without_state_blocks():
    ev = evaluate([PANE1], procs(shell(10), claude(11, 10)), [])
    assert not ev.idle
    assert "not reported" in ev.busy_reasons[0]


def test_stale_state_of_dead_claude_is_dropped_not_trusted():
    # claude crashed without SessionEnd; its BUSY record must not keep the VM up forever
    ev = evaluate([PANE1], procs(shell(10)), [state("pane-1", BUSY, "%1", pid=11)])
    assert ev.idle
    assert ev.stale_keys == ["pane-1"]


def test_stale_state_with_reused_pid_is_dropped():
    p = procs(shell(10), claude(11, 10, start=999))
    ev = evaluate([PANE1], p, [state("pane-1", IDLE, "%1", pid=11, start=500)])
    assert ev.stale_keys == ["pane-1"]
    assert not ev.idle  # the new claude in that pane has not reported yet


def test_stale_idle_state_does_not_hide_busy_shell():
    p = procs(shell(10), other(11, 10, "make"))
    ev = evaluate([PANE1], p, [state("pane-1", IDLE, "%1", pid=99)])
    assert not ev.idle
    assert ev.stale_keys == ["pane-1"]


def test_state_without_pid_is_matched_by_pane():
    p = procs(shell(10), claude(11, 10))
    ev = evaluate([PANE1], p, [state("pane-1", IDLE, "%1", pid=None)])
    assert ev.idle and ev.stale_keys == []
    ev = evaluate([PANE1], procs(shell(10)), [state("pane-1", BUSY, "%1", pid=None)])
    assert ev.idle and ev.stale_keys == ["pane-1"]


def test_busy_claude_outside_tmux_blocks():
    p = procs(claude(50, 1))
    ev = evaluate([], p, [state("session-x", BUSY, None, pid=50)])
    assert not ev.idle


def test_decide_grace_lifecycle():
    assert decide(False, 100.0, 200.0, 600).action == "busy"
    started = decide(True, None, 1000.0, 600)
    assert started.action == "grace-started" and started.idle_since == 1000.0
    mid = decide(True, 1000.0, 1300.0, 600)
    assert mid.action == "grace" and mid.remaining_seconds == 300
    assert decide(True, 1000.0, 1600.0, 600).action == "shutdown"


def test_ready_without_idle_prompt_becomes_idle_after_quiet_period():
    p = procs(shell(10), claude(11, 10))
    st = state("pane-1", READY, "%1", pid=11)
    st.last_event = "Stop"
    st.updated_at = NOW - READY_IDLE_AFTER_SECONDS + 1
    assert not evaluate([PANE1], p, [st]).idle
    st.updated_at = NOW - READY_IDLE_AFTER_SECONDS
    assert evaluate([PANE1], p, [st]).idle


def test_busy_never_times_out():
    p = procs(shell(10), claude(11, 10))
    st = state("pane-1", BUSY, "%1", pid=11)
    st.updated_at = NOW - 10 * READY_IDLE_AFTER_SECONDS
    assert not evaluate([PANE1], p, [st]).idle


def test_running_containers_block_auto_stop():
    p = procs(shell(10))
    ev = evaluate_with_containers([PANE1], p, ["web", "db"])
    assert not ev.idle and "db, web" in ev.busy_reasons[0]
    assert evaluate_with_containers([PANE1], p, []).idle
    assert not evaluate_with_containers([PANE1], p, None).idle


def evaluate_with_containers(panes, procs_, containers):
    from cloud_coder_vm.idle_check import evaluate as real

    return real(panes, procs_, [], NOW, containers)


def test_sg_wrapped_shell_at_prompt_is_idle():
    p = procs(Process(10, 1, "sg", "/usr/bin/sg", 1), shell(11, 10))
    assert evaluate([PANE1], p, []).idle
    p[12] = other(12, 11, "make")
    assert not evaluate([PANE1], p, []).idle


def test_ready_after_stop_failure_waits_for_idle_prompt():
    p = procs(shell(10), claude(11, 10))
    st = state("pane-1", READY, "%1", pid=11)
    st.last_event = "StopFailure"
    st.updated_at = NOW - 10 * READY_IDLE_AFTER_SECONDS
    assert not evaluate([PANE1], p, [st]).idle


def test_restarted_session_without_a_turn_becomes_idle_after_ten_minutes():
    from cloud_coder_vm.idle_check import SESSION_START_IDLE_AFTER_SECONDS

    p = procs(shell(10), claude(11, 10))
    st = state("pane-1", BUSY, "%1", pid=11)
    st.last_event = "SessionStart"
    st.updated_at = NOW - SESSION_START_IDLE_AFTER_SECONDS + 1
    assert not evaluate([PANE1], p, [st]).idle
    st.updated_at = NOW - SESSION_START_IDLE_AFTER_SECONDS
    assert evaluate([PANE1], p, [st]).idle
    st.last_event = "UserPromptSubmit"  # a turn in progress never times out
    assert not evaluate([PANE1], p, [st]).idle
