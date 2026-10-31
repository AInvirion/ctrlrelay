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
import os
import re
import shlex
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

MASK = "***"
_SECRET_NAME = re.compile(r"(?i)key|token|secret|pass|pwd|auth|credential|cookie")
_VALUE = r"(\"[^\"]*\"|'[^']*'|[^\s;&|]+)"
_ASSIGNMENT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)=" + _VALUE)
_FLAG = re.compile(r"(?<![\w-])(--?[A-Za-z][\w-]*)(=|\s+)" + _VALUE)
_TOKEN_SHAPES = [
    re.compile(r"\bBearer\s+[^\s\"']+"),
    re.compile(r"\b(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]+"),
    re.compile(r"\bxox[abp]-[A-Za-z0-9-]+"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
# Long runs that may be a token. A run right after "/" or "." is a path
# segment (a worktree folder is long and hyphenated) and passes, unless it holds
# an unbroken 20+ stretch mixing letters and digits, which words never do.
_RUN = re.compile(r"(?<![A-Za-z0-9+=_-])[A-Za-z0-9+=_-]{20,}")
_UNBROKEN = re.compile(r"[A-Za-z0-9]{20,}")
# Standard base64 carries "/" too; a run starting at, or after, "/" or "." is a path.
_BASE64 = re.compile(r"(?<![\w/.])(?!/)[A-Za-z0-9+/]{32,}={0,2}(?![\w/])")
# A password glued to its flag: `mysql -pSECRET`, `pg_dump -pSECRET`, `sshpass -pSECRET`.
_GLUED_PASSWORD = re.compile(
    r"(\b(?:mysql\w*|mariadb\w*|psql|pg_\w+|sshpass)\b[^;&|\n]*?\s-p)(?=[^\s;&|])[^\s;&|]+"
)
# Commands whose `-p` takes a plugin name, not a password.
_SAFE_DASH_P = frozenset({"pytest", "py.test"})
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1")

# Shell separators that start a new command, and words that only wrap one.
_SEPARATORS = frozenset({"&&", "||", ";", "|", "&", "\n"})
_TEST_RUNNERS = frozenset({"pytest", "jest", "vitest"})
_TEST_SUBCOMMANDS = {"go": "test", "cargo": "test", "playwright": "test"}


def redact(text: str) -> str:
    """Mask anything in a command or pattern that may be a secret."""
    text = re.sub(r"<<.*", "<<\u2026", text, flags=re.S)

    def assignment(m: re.Match) -> str:
        return f"{m.group(1)}={MASK}" if _SECRET_NAME.search(m.group(1)) else m.group(0)

    def flag(m: re.Match) -> str:
        name, sep, value = m.groups()
        if value.startswith("-"):
            return m.group(0)
        if name == "-p" and not _dash_p_is_safe(m.string[: m.start()]):
            return f"{name}{sep}{MASK}"
        if _SECRET_NAME.search(name.lstrip("-")):
            return f"{name}{sep}{MASK}"
        return m.group(0)

    text = _TOKEN_SHAPES[0].sub(f"Bearer {MASK}", text)
    for shape in _TOKEN_SHAPES[1:]:
        text = shape.sub(MASK, text)
    text = _BASE64.sub(MASK, text)
    text = _RUN.sub(_long_run, text)
    text = _GLUED_PASSWORD.sub(lambda m: m.group(1) + MASK, text)
    text = _ASSIGNMENT.sub(assignment, text)
    return _FLAG.sub(flag, text)


def _mixes_letters_and_digits(text: str) -> bool:
    return any(c.isdigit() for c in text) and any(c.isalpha() for c in text)


def _long_run(m: re.Match) -> str:
    run = m.group(0)
    if any(_mixes_letters_and_digits(s) for s in _UNBROKEN.findall(run)):
        return MASK
    in_path = m.start() > 0 and m.string[m.start() - 1] in "/."
    if not in_path and (_mixes_letters_and_digits(run) or len(run) >= 32):
        return MASK
    return run


def _dash_p_is_safe(before: str) -> bool:
    """True when this `-p` belongs to a pytest run, where it names a plugin."""
    segment = re.split(r"&&|\|\||[;|&\n]", before)[-1].split()
    words = _command_words(segment)
    return bool(words) and os.path.basename(words[0]) in _SAFE_DASH_P


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
    command = _strip_heredoc_bodies(command)
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|\n")
    lexer.whitespace = " \t\r"
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
    """Drop env assignments and wrappers (`uv run`, `npx`, `python -m`)."""
    while words:
        head = os.path.basename(words[0])
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        elif head == "uv" and words[1:2] == ["run"]:
            words = words[2:]
        elif head == "npx":
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


def _describe(tool_input: Any) -> str | None:
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "file_path", "pattern"):
        value = tool_input.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(redact(value).split())[:MAX_COMMAND_CHARS]
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
    mid_line = False
    with open(path, "rb") as fh:
        if start > 0:
            fh.seek(start - 1)
            mid_line = fh.read(1) != b"\n"
        fh.seek(start)
        data = fh.read(tail_bytes)
    lines = data.decode("utf-8", errors="replace").split("\n")
    if mid_line:
        lines = lines[1:]  # the cut landed inside a row
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
