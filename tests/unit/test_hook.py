from cloud_coder_vm import session_registry, session_state
from cloud_coder_vm.hook import apply_event, describe
from cloud_coder_vm.process_table import Process
from cloud_coder_vm.session_registry import LogicalSession

ENV = {"TMUX_PANE": "%4", "CLOUD_CODER_SESSION": "cc-app-1"}
CLAUDE = Process(321, 300, "claude", "", 4242)


def run(tmp_path, payload, env=ENV):
    return apply_event(payload, env, tmp_path / "s", tmp_path / "reg.json", CLAUDE)


def test_lifecycle_records_state_by_pane(tmp_path):
    (tmp_path / "s").mkdir()
    run(tmp_path, {"hook_event_name": "SessionStart", "session_id": "A", "source": "startup"})
    st = session_state.load(tmp_path / "s", "pane-4")
    assert (st.state, st.session_id, st.claude_pid, st.claude_starttime) == ("IDLE", "A", 321, 4242)
    run(tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "A"})
    assert session_state.load(tmp_path / "s", "pane-4").state == "BUSY"
    run(
        tmp_path,
        {"hook_event_name": "Stop", "session_id": "A", "background_tasks": [], "session_crons": []},
    )
    assert session_state.load(tmp_path / "s", "pane-4").state == "READY"
    run(
        tmp_path,
        {"hook_event_name": "Notification", "session_id": "A", "notification_type": "idle_prompt"},
    )
    assert session_state.load(tmp_path / "s", "pane-4").state == "IDLE"
    run(tmp_path, {"hook_event_name": "SessionEnd", "session_id": "A", "reason": "other"})
    assert session_state.load(tmp_path / "s", "pane-4") is None


def test_clear_keeps_new_session_when_old_end_arrives_late(tmp_path):
    (tmp_path / "s").mkdir()
    run(tmp_path, {"hook_event_name": "SessionStart", "session_id": "B", "source": "clear"})
    run(tmp_path, {"hook_event_name": "SessionEnd", "session_id": "A", "reason": "clear"})
    assert session_state.load(tmp_path / "s", "pane-4").session_id == "B"


def test_session_start_updates_registry_session_id(tmp_path):
    (tmp_path / "s").mkdir()
    reg = tmp_path / "reg.json"
    session_registry.save(
        reg, {"cc-app-1": LogicalSession("cc-app-1", "app", None, "/w/app", "OLD", 0.0, 0.0)}
    )
    run(tmp_path, {"hook_event_name": "SessionStart", "session_id": "NEW", "source": "clear"})
    assert session_registry.load(reg)["cc-app-1"].claude_session_id == "NEW"


def test_without_tmux_keys_by_session_id(tmp_path):
    (tmp_path / "s").mkdir()
    run(tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "Z"}, env={})
    assert session_state.load(tmp_path / "s", "session-Z").state == "BUSY"


def test_late_session_start_does_not_undo_a_prompt_of_the_same_process(tmp_path):
    (tmp_path / "s").mkdir()
    run(tmp_path, {"hook_event_name": "UserPromptSubmit", "session_id": "A"})
    run(tmp_path, {"hook_event_name": "SessionStart", "session_id": "A", "source": "startup"})
    assert session_state.load(tmp_path / "s", "pane-4").state == "BUSY"


def test_session_start_of_a_new_process_replaces_a_crashed_ones_record(tmp_path):
    (tmp_path / "s").mkdir()
    old = Process(111, 300, "claude", "", 1)
    apply_event(
        {"hook_event_name": "UserPromptSubmit", "session_id": "A"},
        ENV,
        tmp_path / "s",
        tmp_path / "reg.json",
        old,
    )
    run(tmp_path, {"hook_event_name": "SessionStart", "session_id": "A", "source": "resume"})
    st = session_state.load(tmp_path / "s", "pane-4")
    assert (st.state, st.claude_pid, st.last_event) == ("BUSY", 321, "SessionStart")


def test_describe_logs_event_state_and_task_registry():
    stop = {
        "hook_event_name": "Stop",
        "session_id": "A",
        "background_tasks": [{"type": "shell"}, {"type": "agent"}, {"type": "shell"}],
        "session_crons": [],
    }
    assert describe(stop, ENV, "BUSY") == (
        "Stop cc-app-1 (%4) -> BUSY background_tasks=3 (agent,shell) session_crons=0"
    )
    note = {
        "hook_event_name": "Notification",
        "session_id": "A",
        "notification_type": "idle_prompt",
    }
    assert describe(note, {}, "IDLE") == (
        "Notification session A (no tmux) -> IDLE notification_type=idle_prompt"
    )
    end = {"hook_event_name": "SessionEnd", "session_id": "A", "reason": "other"}
    assert describe(end, ENV, None) == "SessionEnd cc-app-1 (%4) -> no state reason=other"
