import json

import pytest

from cloud_coder_vm.session_state import (
    BUSY,
    IDLE,
    READY,
    next_dialogs,
    next_state,
    state_key,
)


def stop(tasks=None, crons=None, missing=False):
    payload = {"hook_event_name": "Stop"}
    if not missing:
        payload["background_tasks"] = tasks or []
        payload["session_crons"] = crons or []
    return payload


IDLE_PROMPT = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}


@pytest.mark.parametrize("current", [None, BUSY, READY, IDLE])
def test_prompt_makes_busy(current):
    assert next_state(current, {"hook_event_name": "UserPromptSubmit"}) == BUSY


def test_fresh_startup_waiting_for_input_is_idle():
    assert next_state(None, {"hook_event_name": "SessionStart", "source": "startup"}) == IDLE


@pytest.mark.parametrize("source", ["resume", "clear", "compact", "fork", None])
def test_other_session_starts_are_busy(source):
    # resume restores session crons; the others may carry over in-flight work
    assert next_state(None, {"hook_event_name": "SessionStart", "source": source}) == BUSY


def test_stop_failure_is_ready_not_idle():
    assert next_state(BUSY, {"hook_event_name": "StopFailure", "error": "rate_limit"}) == READY
    assert next_state(READY, IDLE_PROMPT) == IDLE


def test_stop_without_background_work_is_ready():
    assert next_state(BUSY, stop()) == READY


def test_stop_with_background_task_stays_busy():
    task = {"id": "t1", "type": "subagent", "status": "running"}
    assert next_state(BUSY, stop(tasks=[task])) == BUSY


def test_stop_with_session_cron_stays_busy():
    cron = {"id": "c1", "schedule": "0 9 * * *", "recurring": True, "prompt": "x"}
    assert next_state(BUSY, stop(crons=[cron])) == BUSY


def test_stop_without_task_registry_is_busy_for_safety():
    assert next_state(BUSY, stop(missing=True)) == BUSY


def test_idle_prompt_only_from_ready():
    assert next_state(READY, IDLE_PROMPT) == IDLE
    assert next_state(BUSY, IDLE_PROMPT) == BUSY
    assert next_state(None, IDLE_PROMPT) is None


def test_other_notifications_do_not_change_state():
    perm = {"hook_event_name": "Notification", "notification_type": "permission_prompt"}
    assert next_state(READY, perm) == READY


def test_session_end_removes():
    assert next_state(IDLE, {"hook_event_name": "SessionEnd", "reason": "other"}) is None


def test_state_key_prefers_pane():
    assert state_key("%12", "abc") == "pane-12"
    assert state_key(None, "ab/c-1") == "session-abc-1"


def test_idle_prompt_after_session_start_without_a_turn():
    assert next_state(BUSY, IDLE_PROMPT, last_event="SessionStart") == IDLE
    assert next_state(BUSY, IDLE_PROMPT, last_event="UserPromptSubmit") == BUSY
    assert next_state(BUSY, IDLE_PROMPT, last_event="Stop") == BUSY  # background work


def tool(event, name="Write", path="/w/a.txt"):
    return {"hook_event_name": event, "tool_name": name, "tool_input": {"file_path": path}}


def test_permission_dialog_closes_when_its_own_tool_call_ends():
    shown = next_dialogs([], tool("PermissionRequest"))
    assert len(shown) == 1
    assert next_dialogs(shown, tool("PostToolUse", path="/w/b.txt")) == shown  # another call
    assert next_dialogs(shown, tool("PostToolUse", name="Edit")) == shown
    assert next_dialogs(shown, tool("PostToolUseFailure")) == []
    assert next_dialogs([], tool("PostToolUse")) == []


def test_identical_dialogs_close_one_at_a_time():
    two = next_dialogs(next_dialogs([], tool("PermissionRequest")), tool("PermissionRequest"))
    assert len(next_dialogs(two, tool("PostToolUse"))) == 1


def test_elicitation_closes_with_its_result():
    shown = next_dialogs([], {"hook_event_name": "Elicitation", "mcp_server_name": "a"})
    assert next_dialogs(shown, {"hook_event_name": "ElicitationResult", "mcp_server_name": "b"})
    assert not next_dialogs(shown, {"hook_event_name": "ElicitationResult", "mcp_server_name": "a"})


@pytest.mark.parametrize(
    "payload",
    [
        stop(),
        stop(tasks=[{"type": "shell"}]),  # a background shell asks for nothing
        {"hook_event_name": "SessionStart"},
        {"hook_event_name": "SessionEnd"},
    ],
)
def test_end_of_all_work_closes_every_dialog(payload):
    # a dialog denied with Esc or "No" fires no hook event
    assert next_dialogs(["x"], payload) == []


@pytest.mark.parametrize(
    "payload",
    [
        stop(tasks=[{"type": "shell"}, {"type": "agent"}]),  # an agent may still ask
        stop(tasks=[{"type": "new-kind"}]),
        stop(missing=True),
        {"hook_event_name": "StopFailure"},
        {"hook_event_name": "UserPromptSubmit"},  # a queued prompt does not close one
        IDLE_PROMPT,
    ],
)
def test_other_events_keep_dialogs(payload):
    assert next_dialogs(["x"], payload) == ["x"]


def test_a_prompt_is_received_only_by_a_newer_submission_of_the_same_text():
    from cloud_coder_vm.session_state import SessionState, received_since, record_prompt

    before = SessionState(key="k", session_id="S", state=BUSY)
    record_prompt(before, "go")
    after = SessionState(
        **{**before.__dict__, "recent_prompt_digests": list(before.recent_prompt_digests)}
    )
    assert not received_since(before, after, "go")  # the earlier "go" is not this one
    record_prompt(after, "<task-notification>done</task-notification>")
    assert not received_since(before, after, "go")
    record_prompt(after, '<pasted_content id="1">\n\tgo  on\n</pasted_content>')
    assert received_since(before, after, "\tgo  on  \n")
    record_prompt(after, "<task-notification><status>completed</status></task-notification>")
    assert not received_since(before, after, "status")  # only part of another prompt
    for i in range(10):
        record_prompt(after, f"p{i}")
    assert len(after.recent_prompt_digests) == 8 and after.prompts_submitted == 14
    assert received_since(before, after, "p9") and not received_since(before, after, "go on")


def test_a_record_written_before_prompt_digests_confirms_the_next_prompt(tmp_path):
    """A running Claude Code's record from an agent without recent_prompt_digests."""
    from cloud_coder_vm import session_state

    (tmp_path / "pane-3.json").write_text(
        json.dumps({"key": "pane-3", "session_id": "s", "state": "BUSY", "prompts_submitted": 2})
    )
    before = session_state.load(tmp_path, "pane-3")
    assert before.recent_prompt_digests == []
    after = session_state.load(tmp_path, "pane-3")
    session_state.record_prompt(after, "go")
    assert session_state.received_since(before, after, "go")
