"""Read an agent's phase and last command from its live transcript.

The agent CLI writes a JSON-lines transcript while it works, at
``~/.claude/projects/<worktree path with "/", "." and "_" replaced by "-">/<uuid>.jsonl``.
Each ``assistant`` row carries ``tool_use`` entries (tool name and input) and a
timestamp. This module reads only the tail of the newest transcript, read-only,
and never raises: anything it cannot read comes back as ``Phase.UNKNOWN`` with
a reason.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

MAX_COMMAND_CHARS = 200


class Phase(StrEnum):
    READING = "reading"
    CODING = "coding"
    TESTING = "testing"
    CI = "ci"
    REVIEW = "review"
    PR = "pr"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AgentActivity:
    phase: Phase
    last_command: str | None
    last_step_ts: int | None
    transcript: str | None
    reason: str | None


_READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
_CODE_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
_PR_COMMAND = re.compile(r"\bgit\s+push\b|\bgh\s+pr\s+create\b")
_TEST_COMMAND = re.compile(
    r"\bpytest\b|\bnpm\s+(?:run\s+)?test\b|\bnpx\s+playwright\b|\bgo\s+test\b"
    r"|\bcargo\s+test\b|\bjest\b|\bvitest\b"
)


def transcript_dir_for(worktree_path: str, home: Path) -> Path:
    """The folder the agent CLI writes a worktree's transcripts to."""
    return home / ".claude" / "projects" / re.sub(r"[/._]", "-", worktree_path)


def classify(tool_name: str, tool_input: dict) -> Phase | None:
    """Phase a single tool use belongs to, or None if it says nothing."""
    if tool_name in _READ_TOOLS:
        return Phase.READING
    if tool_name in _CODE_TOOLS:
        return Phase.CODING
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return Phase.READING
        if _PR_COMMAND.search(command):
            return Phase.PR
        if _TEST_COMMAND.search(command):
            return Phase.TESTING
        return Phase.READING
    return None


def _describe(tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "file_path", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())[:MAX_COMMAND_CHARS]
    return None


def _epoch(timestamp: Any) -> int | None:
    if not isinstance(timestamp, str):
        return None
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _tail_lines(path: Path, tail_bytes: int) -> list[str]:
    size = path.stat().st_size
    start = max(0, size - tail_bytes)
    with open(path, "rb") as fh:
        fh.seek(start)
        data = fh.read(tail_bytes)
    lines = data.decode("utf-8", errors="replace").split("\n")
    if start > 0:
        lines = lines[1:]  # began mid-line
    return lines


def _tool_uses(lines: list[str]):
    """Yield (name, input, row timestamp) for every tool use in an assistant row."""
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue  # blank, or a row the CLI is still writing
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") == "tool_use":
                name = item.get("name")
                tool_input = item.get("input")
                if isinstance(name, str):
                    yield name, tool_input if isinstance(tool_input, dict) else {}, row.get(
                        "timestamp"
                    )


def _unknown(reason: str, transcript: str | None = None) -> AgentActivity:
    return AgentActivity(
        phase=Phase.UNKNOWN,
        last_command=None,
        last_step_ts=None,
        transcript=transcript,
        reason=reason,
    )


def read_activity(
    worktree_path: str | None,
    *,
    home: Path | None = None,
    tail_bytes: int = 262_144,
) -> AgentActivity:
    """What the agent working in ``worktree_path`` did last, from its transcript."""
    if not worktree_path:
        return _unknown("no worktree")
    folder = transcript_dir_for(worktree_path, home if home is not None else Path.home())
    try:
        candidates = [p for p in folder.glob("*.jsonl") if p.is_file()]
        if not candidates:
            return _unknown("no transcript")
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        lines = _tail_lines(newest, tail_bytes)
    except OSError as exc:
        return _unknown(f"transcript unreadable: {exc.__class__.__name__}")

    phase: Phase | None = None
    last_command: str | None = None
    last_step_ts: int | None = None
    for name, tool_input, timestamp in _tool_uses(lines):
        last_step_ts = _epoch(timestamp)
        described = _describe(tool_input)
        if described is not None:
            last_command = described
        classified = classify(name, tool_input)
        if classified is not None:
            phase = classified

    if phase is None:
        return AgentActivity(
            phase=Phase.UNKNOWN,
            last_command=last_command,
            last_step_ts=last_step_ts,
            transcript=str(newest),
            reason="no step in transcript tail",
        )
    return AgentActivity(
        phase=phase,
        last_command=last_command,
        last_step_ts=last_step_ts,
        transcript=str(newest),
        reason=None,
    )
