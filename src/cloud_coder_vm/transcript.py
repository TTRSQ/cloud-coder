"""Claude Code's transcript of a conversation: ~/.claude/projects/<cwd>/<session id>.jsonl.

Claude Code appends one JSON object per line: prompts, tool results, attachments and
bookkeeping entries, and one ``assistant`` line per content block of each API response
(thinking, text and tool_use blocks of one response share ``message.id``). The format is
Claude Code's own and undocumented beyond the file's location, so anything unexpected in
a line is skipped rather than failed on.
"""

import json
from pathlib import Path


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


def last_assistant_message(path: Path) -> dict | None:
    """The text of Claude Code's latest response that has text, with the time it was
    written: the text blocks of one API response joined. Thinking and tool calls are left
    out, so while a turn runs only tool calls this is the text written before them. None
    when the conversation has no such response yet."""
    texts: list[str] = []
    message_id = timestamp = None
    for line in reversed(path.read_bytes().splitlines()):
        entry = _entry(line)
        if entry is None:
            continue
        if entry.get("type") == "user" and not entry.get("isSidechain") and message_id:
            break  # the response's blocks all come before the next prompt or tool result
        message = _assistant_message(entry)
        if message is None:
            continue
        if message_id is not None and message.get("id") != message_id:
            break
        text = _text(message)
        if message_id is None:
            if not text:
                continue
            message_id = message.get("id") or object()  # no id: this line alone
            timestamp = entry.get("timestamp")
        if text:
            texts.append(text)
    if not texts:
        return None
    return {"text": "\n\n".join(reversed(texts)), "timestamp": timestamp}


def last_message(home: Path, claude_session_id: str) -> dict | None:
    """``last_assistant_message`` of a conversation; None also when it has no transcript
    (Claude Code writes it with the first prompt)."""
    path = find(home, claude_session_id)
    return last_assistant_message(path) if path is not None else None
