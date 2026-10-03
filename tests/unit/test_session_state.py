import pytest

from cloud_coder_vm.session_state import BUSY, IDLE, READY, next_state, state_key


def stop(tasks=None, crons=None, missing=False):
    payload = {"hook_event_name": "Stop"}
    if not missing:
        payload["background_tasks"] = tasks or []
        payload["session_crons"] = crons or []
    return payload


IDLE_PROMPT = {"hook_event_name": "Notification", "notification_type": "idle_prompt"}


@pytest.mark.parametrize("current", [None, BUSY, READY, IDLE])
@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit"])
def test_start_and_prompt_make_busy(current, event):
    assert next_state(current, {"hook_event_name": event}) == BUSY


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
