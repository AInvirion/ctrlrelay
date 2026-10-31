"""Read an agent's phase and last command from its live transcript.

The agent CLI writes a JSON-lines transcript while it works, at
``~/.claude/projects/<worktree path with "/", "." and "_" replaced by "-">/<uuid>.jsonl``.
Each ``assistant`` row carries ``tool_use`` entries (tool name and input) and a
timestamp. This module reads only the tail of the newest transcript, read-only,
and never raises: anything it cannot read comes back as ``Phase.UNKNOWN`` with
a reason.

The page is open to the home network, so the last command is never shown as
typed. It is a summary built only from words known to be safe to show: program
names and short lowercase subcommands, a path relative to the worktree, or the
word "search". Everything else is left out and marked with "…".
"""

from __future__ import annotations

import json
import os
import re
import shlex
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

MAX_COMMAND_CHARS = 200
HIDDEN = "***"
MORE = "…"


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
_SEARCH_TOOLS = frozenset({"Grep", "Glob"})
_FILE_TOOLS = frozenset({"Read", "Edit", "Write", "NotebookEdit", "MultiEdit"})

_SECRET_WORD = re.compile(r"(?i)key|token|secret|pass|pwd|auth|credential|cookie")
_SHOWN_WORD = re.compile(r"[a-z][a-z0-9._:-]{0,30}")
# Words that fit the shape above and are still credentials (`xoxs-abc`, `sk-abc`).
_TOKEN_PREFIX = re.compile(r"(?i)(?:sk|xox[a-z]|gh[a-z]|github_pat|akia)[-_]")
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")
_FD_REDIRECT = re.compile(r"\d*>&\d+")

# Shell separators that start a new command, and words that only wrap one.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})
_TEST_RUNNERS = frozenset({"pytest", "jest", "vitest"})
_TEST_SUBCOMMANDS = {"go": "test", "cargo": "test", "playwright": "test"}


def _strip_heredoc_bodies(command: str) -> str:
    lines = command.split("\n")
    kept: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        kept.append(line)
        i += 1
        for m in _HEREDOC.finditer(line):
            while i < len(lines) and lines[i].strip() != m.group(2):
                i += 1
            i += 1  # the terminator
    return "\n".join(kept)


def _segments(command: str) -> list[list[str]]:
    """The simple commands in a shell line, as word lists, quotes respected."""
    command = _FD_REDIRECT.sub(" ", _strip_heredoc_bodies(command))
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True  # words such as `test:unit` stay whole
    try:
        tokens = list(lexer)
    except ValueError:  # unbalanced quote: fall back to a plain split
        tokens = re.split(r"\s+|(&&|\|\||[;|&\n])", command)
        tokens = [t for t in tokens if t]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _command_words(words: list[str]) -> list[str]:
    """Drop env assignments and wrappers (`uv run`, `poetry run`, `npx`, `python -m`)."""
    while words:
        head = os.path.basename(words[0])
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        elif head in ("uv", "poetry") and words[1:2] == ["run"]:
            words = words[2:]
        elif head in ("npx", "bunx"):
            words = words[1:]
        elif re.fullmatch(r"python[0-9.]*", head) and words[1:2] == ["-m"]:
            words = words[2:]
        else:
            break
    return words


def _classify_segment(words: list[str]) -> Phase | None:
    words = _command_words(words)
    if not words:
        return None
    head = os.path.basename(words[0])
    rest = words[1:]
    if head in _TEST_RUNNERS:
        return Phase.TESTING
    if head in _TEST_SUBCOMMANDS and rest[:1] == [_TEST_SUBCOMMANDS[head]]:
        return Phase.TESTING
    if head == "npm" and (rest[:1] == ["test"] or (rest[:1] == ["run"] and rest[1:2]
                                                  and rest[1].startswith("test"))):
        return Phase.TESTING
    if head == "git" and rest[:1] == ["push"]:
        return Phase.PR
    if head == "gh" and rest[:2] == ["pr", "create"]:
        return Phase.PR
    return None


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
        phase = Phase.READING  # any other shell command only looks around
        for words in _segments(command):
            classified = _classify_segment(words)
            if classified is not None:
                phase = classified
        return phase
    return None


def _is_shown(word: str) -> bool:
    return (
        _SHOWN_WORD.fullmatch(word) is not None
        and not _SECRET_WORD.search(word)
        and not _TOKEN_PREFIX.match(word)
    )


def summarize_command(command: str) -> str:
    """Program names and up to two safe subcommand words per shell segment."""
    parts: list[str] = []
    for segment in _segments(command):
        words = _command_words(segment)
        if not words or os.path.basename(words[0]) == "cd":
            continue
        program = os.path.basename(words[0])
        shown = [program if _is_shown(program) else HIDDEN]
        rest = words[1:]
        while rest and len(shown) < 3 and _is_shown(rest[0]):
            shown.append(rest.pop(0))
        if rest:
            shown.append(MORE)
        parts.append(" ".join(shown))
    return " && ".join(parts) if parts else MORE


def summarize_path(file_path: str, worktree_path: str | None) -> str:
    """The path inside the worktree, or only the file name when outside it."""
    path = PurePosixPath(file_path)
    if worktree_path and path.is_relative_to(worktree_path):
        parts = path.relative_to(worktree_path).parts
    else:
        parts = (path.name,)
    return "/".join(HIDDEN if _SECRET_WORD.search(p) else p for p in parts) or MORE


def _describe(tool_name: str, tool_input: dict, worktree_path: str | None) -> str | None:
    if tool_name in _SEARCH_TOOLS:
        return "search"
    if tool_name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return summarize_command(command)[:MAX_COMMAND_CHARS]
        return None
    if tool_name in _FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(path, str) and path.strip():
            return summarize_path(path, worktree_path)[:MAX_COMMAND_CHARS]
    return None


def _epoch(timestamp: Any) -> int | None:
    if not isinstance(timestamp, str):
        return None
    try:
        return int(datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _tail_lines(path: Path, tail_bytes: int) -> list[str]:
    """The complete lines in the last ``tail_bytes`` bytes, reading no more than that."""
    size = path.stat().st_size
    count = max(0, min(tail_bytes, size))
    with open(path, "rb") as fh:
        fh.seek(size - count)
        data = fh.read(count)
    lines = data.decode("utf-8", errors="replace").split("\n")
    if count < size:
        # The first byte read is only there to show where a row begins: a cut
        # landing just after a newline keeps the row that follows it.
        lines = lines[1:]
    return lines


def _tool_uses(lines: list[str]):
    """Yield (name, input, row timestamp) for every tool use in an assistant row."""
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue  # blank, half-written, or too deep to parse
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
    try:
        folder = transcript_dir_for(worktree_path, home if home is not None else Path.home())
        candidates = [p for p in folder.glob("*.jsonl") if p.is_file()]
        if not candidates:
            return _unknown("no transcript")
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        lines = _tail_lines(newest, tail_bytes)
    except Exception as exc:
        return _unknown(f"transcript unreadable: {exc.__class__.__name__}")

    phase: Phase | None = None
    last_command: str | None = None
    last_step_ts: int | None = None
    for name, tool_input, timestamp in _tool_uses(lines):
        last_step_ts = _epoch(timestamp)
        try:
            described = _describe(name, tool_input, worktree_path)
            classified = classify(name, tool_input)
        except Exception:
            described, classified = MORE, None
        if described is not None:
            last_command = described
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
