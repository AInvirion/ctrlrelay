"""Read an agent's phase and last command from its live transcript.

The agent CLI writes a JSON-lines transcript while it works, at
``~/.claude/projects/<worktree path with "/", "." and "_" replaced by "-">/<uuid>.jsonl``.
Each ``assistant`` row carries ``tool_use`` entries (tool name and input) and a
timestamp. This module reads only the tail of the newest transcript, read-only,
and never raises: anything it cannot read comes back as ``Phase.UNKNOWN`` with
a reason.

The page is open to the home network, so the last command is never shown as
typed. It is built only from fixed lists: a known program name (anything else is
"cmd"), one known subcommand for a few tools, a path inside the worktree (any
component of it that looks like a credential shown as "***"), or the word
"search". Everything else is left out and marked with "…".
"""

from __future__ import annotations

import io
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

# The only words a shell summary is built from.
_PROGRAMS = frozenset({
    "git", "gh", "pytest", "python", "python3", "uv", "pip", "npm", "npx", "node",
    "yarn", "pnpm", "bun", "ruff", "mypy", "make", "cargo", "go", "rustc", "docker",
    "ls", "cat", "head", "tail", "sed", "grep", "rg", "find", "wc", "diff", "mkdir",
    "cp", "mv", "rm", "touch", "curl", "jq", "playwright", "jest", "vitest", "tsc",
    "eslint", "prettier", "ctrlrelay",
})
_PACKAGE_SUBCOMMANDS = frozenset({"test", "run", "install", "ci"})
_SUBCOMMANDS = {
    "git": frozenset({
        "status", "diff", "log", "show", "add", "commit", "push", "pull", "fetch",
        "checkout", "switch", "rebase", "merge", "branch", "stash", "restore",
    }),
    "gh": frozenset({"pr", "issue", "run", "api", "repo"}),
    "npm": _PACKAGE_SUBCOMMANDS,
    "pnpm": _PACKAGE_SUBCOMMANDS,
    "yarn": _PACKAGE_SUBCOMMANDS,
    "uv": frozenset({"run", "sync", "pip", "add"}),
    "cargo": frozenset({"test", "build", "run", "check"}),
    "go": frozenset({"test", "build", "run", "vet"}),
    "docker": frozenset({"build", "run", "compose", "ps", "logs"}),
}
UNKNOWN_PROGRAM = "cmd"
OUTSIDE_REPO = "file outside repo"
# Shell operator characters. shlex returns a run of them as one token (";\n",
# "|&", ">&"); an unquoted run made only of _CONTROL characters ends a command.
_PUNCTUATION = "();<>|&\n"
_CONTROL = frozenset(";&|\n()")
# `env` options that take an operand, and ones that do not.
_ENV_OPERAND_FLAGS = frozenset({"-u", "--unset", "-C", "--chdir"})
_TEST_RUNNERS = frozenset({"pytest", "jest", "vitest"})
_TEST_SUBCOMMANDS = {"go": "test", "cargo": "test", "playwright": "test"}
# Shell commands known to only look (READING) or to change files (CODING).
# Anything not listed says nothing about the phase.
_READ_PROGRAMS = frozenset({
    "cat", "head", "tail", "less", "ls", "grep", "rg", "find", "wc", "diff",
    "tree", "stat", "file",
})
_WRITE_PROGRAMS = frozenset({
    "rm", "mv", "cp", "touch", "mkdir", "rmdir", "chmod", "ln", "tee", "patch",
})
_GIT_READ = frozenset({"status", "diff", "log", "show", "blame"})
_GIT_WRITE = frozenset({
    "add", "commit", "checkout", "switch", "restore", "stash", "rebase", "merge",
    "reset", "cherry-pick", "apply",
})
_GH_READ = frozenset({("pr", "view"), ("pr", "list"), ("issue", "view"), ("issue", "list")})
_INSTALLERS = {
    "npm": frozenset({"install", "add"}),
    "pnpm": frozenset({"install", "add"}),
    "yarn": frozenset({"install", "add"}),
    "uv": frozenset({"add", "sync"}),
    "pip": frozenset({"install"}),
}


def _shell_tokens(command: str):
    """Yield (token, is_operator) with heredoc bodies left out.

    Operators come out as tokens of their own; quoted or escaped text, even ";"
    or "<<", is a word. A heredoc's body is read off the raw input and skipped,
    so its text never reaches the lexer.
    """
    stream = io.StringIO(command)
    lexer = shlex.shlex(stream, posix=True, punctuation_chars=_PUNCTUATION)
    lexer.whitespace = " \t\r"  # newline is an operator, not a space
    lexer.whitespace_split = True  # words such as `test:unit` stay whole

    def offset() -> int:
        # shlex holds back at most one character it has read but not used.
        return stream.tell() - len(lexer._pushback_chars)

    delimiters: list[str] = []
    while True:
        start = offset()
        token = lexer.get_token()
        if token is None:
            return
        raw = command[start:offset()].strip(" \t\r")
        operator = bool(token) and raw == token and set(token) <= set(_PUNCTUATION)
        if operator and token == "<<":
            delimiter = lexer.get_token()
            if delimiter is None:
                return
            delimiters.append(delimiter[1:] if delimiter.startswith("-") else delimiter)
            continue
        yield token, operator
        if operator and "\n" in token and delimiters:
            pending = token[token.rindex("\n") + 1 :] + "".join(reversed(lexer._pushback_chars))
            lexer._pushback_chars.clear()
            for delimiter in delimiters:
                while True:
                    line = pending + stream.readline()
                    pending = ""
                    if not line or line.strip() == delimiter:
                        break
            delimiters = []


def _segments(command: str) -> list[list[str]]:
    """The simple commands in a shell line, as word lists."""
    try:
        tokens = list(_shell_tokens(command))
    except ValueError:  # unbalanced quote: fall back to a plain split
        tokens = [
            (t, set(t) <= _CONTROL)
            for t in re.split(r"[ \t\r]+|([;&|\n]+)", command)
            if t
        ]
    segments: list[list[str]] = [[]]
    for token, operator in tokens:
        if operator and set(token) <= _CONTROL:
            segments.append([])
        else:
            segments[-1].append(token)
    return [s for s in segments if s]


def _command_words(words: list[str]) -> list[str]:
    """Drop env assignments and wrappers (`env`, `uv run`, `poetry run`, `npx`, `python -m`)."""
    while words:
        head = os.path.basename(words[0])
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
            words = words[1:]
        elif head == "env":
            words = words[1:]
            while words and words[0].startswith("-"):
                flag = words[0]
                words = words[2:] if flag in _ENV_OPERAND_FLAGS else words[1:]
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
    return _read_or_write(head, rest)


def _read_or_write(head: str, rest: list[str]) -> Phase | None:
    """READING for a known read-only command, CODING for a known mutating
    one, None for anything else."""
    if head == "sed":
        in_place = any(
            re.match(r"-[A-Za-z]*i", w) or w.startswith("--in-place") for w in rest
        )
        return Phase.CODING if in_place else Phase.READING
    if head in _READ_PROGRAMS:
        return Phase.READING
    if head in _WRITE_PROGRAMS:
        return Phase.CODING
    if head == "git":
        if (rest[:1] and rest[0] in _GIT_READ) or rest == ["branch"]:
            return Phase.READING
        if rest[:1] and rest[0] in _GIT_WRITE:
            return Phase.CODING
        return None
    if head == "gh":
        return Phase.READING if tuple(rest[:2]) in _GH_READ else None
    if head == "uv" and rest[:2] == ["pip", "install"]:
        return Phase.CODING
    if rest[:1] and rest[0] in _INSTALLERS.get(head, ()):
        return Phase.CODING
    return None


def transcript_dir_for(worktree_path: str, home: Path) -> Path:
    """The folder the agent CLI writes a worktree's transcripts to."""
    return home / ".claude" / "projects" / re.sub(r"[/._]", "-", worktree_path)


def classify(tool_name: str, tool_input: dict) -> Phase | None:
    """Phase a single tool use belongs to, or None if it says nothing.

    For a shell command the phase is a best guess from the program and its
    subcommand; the arguments are not inspected, so ``find -delete`` reads
    as reading and ``git branch -D`` says nothing.
    """
    if tool_name in _READ_TOOLS:
        return Phase.READING
    if tool_name in _CODE_TOOLS:
        return Phase.CODING
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str):
            return None
        # The last segment that says something decides, except that a read
        # never outranks anything else the line did: `pytest | tail` is
        # testing, `rm x && ls` is coding. A line made only of commands
        # nobody listed says nothing (None), so the previous phase stands.
        phase: Phase | None = None
        reads = False
        for words in _segments(command):
            classified = _classify_segment(words)
            if classified is Phase.READING:
                reads = True
            elif classified is not None:
                phase = classified
        if phase is None and reads:
            return Phase.READING
        return phase
    return None


def summarize_command(command: str, limit: int = MAX_COMMAND_CHARS) -> str:
    """Each shell segment as a known program and, for some, one known subcommand.

    Over ``limit`` characters, whole trailing segments are dropped and "…" added,
    so no word is ever cut in half.
    """
    parts: list[str] = []
    for segment in _segments(command):
        words = _command_words(segment)
        if not words or os.path.basename(words[0]) == "cd":
            continue
        program = os.path.basename(words[0])
        rest = words[1:]
        if program not in _PROGRAMS:
            shown = [UNKNOWN_PROGRAM]
        else:
            shown = [program]
            if rest and rest[0] in _SUBCOMMANDS.get(program, ()):
                shown.append(rest.pop(0))
        if rest:
            shown.append(MORE)
        parts.append(" ".join(shown))
    text = " && ".join(parts) if parts else MORE
    while len(text) > limit and parts:
        parts.pop()
        text = " && ".join(parts) + " " + MORE if parts else MORE
    return text[:limit]


# A path component that looks like a credential, or names a file that
# holds one, is shown as MASK. A token prefix counts only at the start of
# the component or after a non-alphanumeric, so "task-runner" is not "sk-".
_TOKEN_PREFIX = re.compile(
    r"(?<![A-Za-z0-9])(?:ghp_|gho_|ghs_|github_pat_|sk-|xox[a-z]-|AKIA)"
)
_LONG_RUN = re.compile(r"[A-Za-z0-9]{20,}")
_SECRET_NAME = re.compile(
    r"(?i)(secret|token|passw|credential|api[_-]?key|apikey|private|\.env$"
    r"|\.pem$|\.key$|\.p12$|\.pfx$|id_rsa|id_ed25519|\.netrc|\.npmrc|\.pypirc)"
)
MASK = "***"


def _looks_secret(component: str) -> bool:
    if component.startswith(".env"):
        return True
    if _TOKEN_PREFIX.search(component) or _SECRET_NAME.search(component):
        return True
    return any(
        re.search(r"[A-Za-z]", run) and re.search(r"[0-9]", run)
        for run in _LONG_RUN.findall(component)
    )


def summarize_path(file_path: str, worktree_path: str | None) -> str:
    """The path relative to the worktree, or a fixed phrase when it is outside.

    Any component that looks like a credential (see ``_looks_secret``) is
    replaced by ``***``; the rest of the path is shown as it is.
    """
    if not worktree_path:
        return OUTSIDE_REPO
    path = os.path.realpath(file_path)
    root = os.path.realpath(worktree_path)
    if os.path.commonpath([path, root]) != root or path == root:
        return OUTSIDE_REPO
    parts = os.path.relpath(path, root).split(os.sep)
    return os.sep.join(MASK if _looks_secret(p) else p for p in parts)


def _describe(tool_name: str, tool_input: dict, worktree_path: str | None) -> str | None:
    if tool_name in _SEARCH_TOOLS:
        return "search"
    if tool_name == "Bash":
        command = tool_input.get("command")
        if isinstance(command, str) and command.strip():
            return summarize_command(command)
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
    """The lines at the end of the file, reading at most ``tail_bytes`` bytes in all.

    When the file is longer than that, the first byte read is a probe: the byte
    just before the tail. The tail's first line is a whole row only if the probe
    is a newline; otherwise it is the end of a row and is dropped.
    """
    size = path.stat().st_size
    count = max(0, min(tail_bytes, size))
    with open(path, "rb") as fh:
        fh.seek(size - count)
        data = fh.read(count)
    probe = b"\n"
    if count < size:
        probe, data = data[:1], data[1:]
    lines = data.decode("utf-8", errors="replace").split("\n")
    if probe != b"\n":
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
