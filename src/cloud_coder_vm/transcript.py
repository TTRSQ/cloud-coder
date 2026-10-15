"""Claude Code's transcript of a conversation: ~/.claude/projects/<cwd>/<session id>.jsonl.

Claude Code appends one JSON object per line: prompts, tool results, attachments and
bookkeeping entries, and one ``assistant`` line per content block of each API response
(thinking, text and tool_use blocks of one response share ``message.id``). The format is
Claude Code's own and undocumented beyond the file's location, so anything unexpected in
a line is skipped rather than failed on.

The Stop hook's ``last_assistant_message`` is not used instead: it comes only at the end
of a turn, and the hook's state lives on tmpfs, gone once the VM stops by itself; the
transcript is on the Persistent Disk.
"""

import json
from pathlib import Path

# the most responses one read returns
MAX_MESSAGES = 10


def find(home: Path, claude_session_id: str) -> Path | None:
    """The transcript of a conversation; the most recently written one when a resume in
    another directory left one in each project directory."""
    found = list((home / ".claude/projects").glob(f"*/{claude_session_id}.jsonl"))
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def _entry(line: bytes) -> dict | None:
    try:
        entry = json.loads(line)
    except ValueError:  # a line being written, or a damaged one
        return None
    return entry if isinstance(entry, dict) else None


def _assistant_message(entry: dict) -> dict | None:
    """The API message of an assistant line of the main conversation (not a subagent's)."""
    message = entry.get("message")
    if (
        entry.get("type") != "assistant"
        or entry.get("isSidechain")
        or not isinstance(message, dict)
    ):
        return None
    return message


def _text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and block.get("type") == "text" and block.get("text")
    )


def assistant_messages(path: Path, count: int) -> list[dict]:
    """Claude Code's latest ``count`` responses that have text, oldest first, each with
    the time it was written: the text blocks of one API response joined. Thinking and
    tool calls are left out, so while a turn runs only tool calls the newest one is the
    text written before them, which may be the answer to an earlier prompt."""
    responses: list[dict] = []  # newest first
    current: dict | None = None  # the response whose blocks are being collected
    for line in reversed(path.read_bytes().splitlines()):
        entry = _entry(line)
        message = _assistant_message(entry) if entry is not None else None
        if message is None:
            continue
        message_id = message.get("id")
        if current is not None and (message_id is None or message_id != current["id"]):
            current = None  # a line without an id is a response of its own
        text = _text(message)
        if current is None:
            if not text:
                continue
            if len(responses) == count:
                break
            current = {"id": message_id, "texts": [], "timestamp": entry.get("timestamp")}
            responses.append(current)
        if text:
            current["texts"].append(text)
    return [
        {"text": "\n\n".join(reversed(r["texts"])), "timestamp": r["timestamp"]}
        for r in reversed(responses)
    ]


def messages(home: Path, claude_session_id: str, count: int) -> list[dict]:
    """``assistant_messages`` of a conversation; [] also when it has no transcript
    (Claude Code writes it with the first prompt) or the transcript cannot be read."""
    try:
        path = find(home, claude_session_id)
        return assistant_messages(path, count) if path is not None else []
    except OSError:  # removed or unreadable while being read
        return []
