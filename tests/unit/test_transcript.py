import json
import os

from cloud_coder_vm import transcript

SID = "cfe692ba-adac-4961-9dec-80872c14e625"


def prompt(text, **extra):
    return {
        "type": "user",
        "isSidechain": False,
        "promptSource": "typed",
        "message": {"role": "user", "content": text},
        "timestamp": "2026-10-15T02:31:38.000Z",
        "sessionId": SID,
        **extra,
    }


def block(message_id, block, timestamp="2026-10-15T02:32:04.000Z", **extra):
    """One assistant line: Claude Code writes each content block of a response alone."""
    return {
        "type": "assistant",
        "isSidechain": False,
        "requestId": "req_" + message_id,
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": "claude-opus-5-5",
            "content": [block],
            "stop_reason": "end_turn",
        },
        "timestamp": timestamp,
        "sessionId": SID,
        **extra,
    }


def thinking(message_id):
    return block(message_id, {"type": "thinking", "thinking": "", "signature": "sig"})


def text(message_id, value, **kw):
    return block(message_id, {"type": "text", "text": value}, **kw)


def tool_use(message_id, name="Agent"):
    return block(message_id, {"type": "tool_use", "id": "toolu_1", "name": name, "input": {}})


def tool_result():
    return {
        "type": "user",
        "isSidechain": False,
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "ok"}],
        },
    }


BOOKKEEPING = [
    {"type": "attachment", "attachment": {"type": "environment"}},
    {"type": "system", "subtype": "stop_hook_summary"},
    {"type": "system", "subtype": "turn_duration", "isMeta": False},
    {"type": "last-prompt", "lastPrompt": "fix it"},
    {"type": "queue-operation", "operation": "enqueue"},
]


def last(path):
    """The newest response, or None."""
    responses = transcript.assistant_messages(path, 1)
    assert len(responses) <= 1
    return responses[0] if responses else None


def write(tmp_path, entries, tail=""):
    path = tmp_path / f"{SID}.jsonl"
    path.write_text(
        "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries) + tail,
        encoding="utf-8",
    )
    return path


def test_last_response_text_after_thinking_and_bookkeeping(tmp_path):
    path = write(
        tmp_path,
        [
            prompt("調べて"),
            thinking("msg_1"),
            tool_use("msg_1"),
            tool_result(),
            thinking("msg_2"),
            text("msg_2", "調査が終わりました。", timestamp="2026-10-15T02:40:50.000Z"),
            *BOOKKEEPING,
        ],
    )
    assert last(path) == {
        "text": "調査が終わりました。",
        "timestamp": "2026-10-15T02:40:50.000Z",
    }


def test_text_blocks_of_one_response_are_joined_in_order(tmp_path):
    path = write(
        tmp_path,
        [
            prompt("go"),
            text("msg_0", "an earlier answer"),
            prompt("next"),
            thinking("msg_1"),
            text("msg_1", "first part", timestamp="t1"),
            tool_use("msg_1"),
            text("msg_1", "second part", timestamp="t2"),
        ],
    )
    assert last(path) == {
        "text": "first part\n\nsecond part",
        "timestamp": "t2",
    }


def test_tool_only_responses_leave_the_text_written_before_them(tmp_path):
    path = write(
        tmp_path,
        [
            prompt("go"),
            text("msg_1", "Starting with the tests."),
            tool_use("msg_1"),
            tool_result(),
            thinking("msg_2"),
            tool_use("msg_2", "Bash"),
            tool_result(),
        ],
    )
    assert last(path)["text"] == "Starting with the tests."


def test_blocks_of_one_response_around_a_tool_result_are_joined(tmp_path):
    path = write(
        tmp_path,
        [text("msg_1", "part 1"), tool_use("msg_1"), tool_result(), text("msg_1", "part 2")],
    )
    assert last(path)["text"] == "part 1\n\npart 2"


def test_a_text_of_another_response_before_a_tool_result_is_not_joined(tmp_path):
    path = write(
        tmp_path,
        [text("msg_1", "old"), tool_use("msg_1"), tool_result(), text("msg_2", "new")],
    )
    assert last(path)["text"] == "new"


def test_subagent_lines_are_skipped(tmp_path):
    path = write(
        tmp_path,
        [
            text("msg_1", "main answer"),
            prompt("subagent task", isSidechain=True),
            text("msg_9", "subagent answer", isSidechain=True),
        ],
    )
    assert last(path)["text"] == "main answer"


def test_partial_and_damaged_lines_are_skipped(tmp_path):
    path = write(
        tmp_path,
        [text("msg_1", "done")],
        tail='not json\n{"type": "assistant", "message": {"id": "msg_2", "content": [{"ty',
    )
    assert last(path)["text"] == "done"


def test_string_content_and_missing_ids(tmp_path):
    no_id = text("msg_x", "API Error: overloaded")
    del no_id["message"]["id"]
    older = text("msg_y", "older")
    del older["message"]["id"]
    string = text("msg_s", "")
    string["message"]["content"] = "plain string"
    assert last(write(tmp_path, [older, no_id]))["text"] == ("API Error: overloaded")
    assert last(write(tmp_path, [string]))["text"] == "plain string"


def test_no_response_yet(tmp_path):
    assert last(write(tmp_path, [prompt("hi"), thinking("m")])) is None
    assert last(write(tmp_path, [])) is None


def test_the_latest_responses_oldest_first(tmp_path):
    path = write(
        tmp_path,
        [
            prompt("one"),
            text("msg_1", "answer 1", timestamp="t1"),
            prompt("two"),
            thinking("msg_2"),
            text("msg_2", "answer 2a", timestamp="t2a"),
            tool_use("msg_2"),
            tool_result(),
            thinking("msg_3"),
            tool_use("msg_3", "Bash"),
            tool_result(),
            text("msg_4", "answer 2b", timestamp="t2b"),
            text("msg_9", "subagent", isSidechain=True),
            *BOOKKEEPING,
        ],
    )
    assert transcript.assistant_messages(path, 2) == [
        {"text": "answer 2a", "timestamp": "t2a"},
        {"text": "answer 2b", "timestamp": "t2b"},
    ]
    assert [m["text"] for m in transcript.assistant_messages(path, 10)] == [
        "answer 1",
        "answer 2a",
        "answer 2b",
    ]
    assert transcript.assistant_messages(write(tmp_path, [prompt("hi")]), 3) == []


def test_lines_without_an_id_are_responses_of_their_own(tmp_path):
    first, second = text("x", "e1"), text("x", "e2")
    del first["message"]["id"], second["message"]["id"]
    path = write(
        tmp_path,
        [
            first,
            second,
            text("m1", "x1"),
            prompt("go"),
            text("m1", "x2"),
            tool_use("m2"),
            text("m1", "x3"),
        ],
    )
    assert [m["text"] for m in transcript.assistant_messages(path, 5)] == [
        "e1",
        "e2",
        "x1\n\nx2",
        "x3",
    ]


def test_find_and_messages(tmp_path):
    projects = tmp_path / ".claude/projects"
    assert transcript.find(tmp_path, SID) is None
    assert transcript.messages(tmp_path, SID, 1) == []

    old = projects / "-home-coder-git-a" / f"{SID}.jsonl"
    new = projects / "-home-coder-git-wt-a-2" / f"{SID}.jsonl"
    for path, answer in ((old, "old"), (new, "new")):
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(text("m", answer)) + "\n")
    os.utime(old, (1, 1))
    (projects / "-home-coder-git-a" / "other-session.jsonl").write_text("")
    assert transcript.find(tmp_path, SID) == new
    assert transcript.messages(tmp_path, SID, 3) == [
        {"text": "new", "timestamp": "2026-10-15T02:32:04.000Z"}
    ]


def test_an_unreadable_transcript_is_no_message(tmp_path, monkeypatch):
    path = tmp_path / ".claude/projects/-w" / f"{SID}.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(text("m", "answer")) + "\n")

    def removed(self):
        raise FileNotFoundError(self)

    monkeypatch.setattr(transcript.Path, "read_bytes", removed)
    assert transcript.messages(tmp_path, SID, 1) == []
