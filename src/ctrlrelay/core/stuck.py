"""Notice a run that is going in circles, and say why (#224).

Before this, the only thing that stopped a stuck agent was the overall
timeout, and a stuck run looked exactly like a slow one until then. Two
signals are read here, both pure functions so they can be tested without
a process or a forge:

- **The live transcript.** The agent CLI writes a JSON-lines transcript as
  it works (see :mod:`ctrlrelay.web.transcript`). :func:`detect_loop`
  reads its tool calls and their results and reports the agent running the
  same command over and over, or getting the same error back from the same
  command over and over with nothing it tried in between changing it.
  :func:`watch_transcript` polls the transcript while the agent runs; the
  dispatcher stops the agent when it returns.

- **CI between fix rounds.** :func:`classify_failure` sorts a failed CI
  log into a :class:`FailureKind`, so only a failure a retry can help gets
  one: flaky infrastructure gets the job re-run, a real code failure goes
  to the agent, a job that ran out of time is not retried at all.
  :func:`failed_tests` names the failing tests, so the verify loop can see
  the same tests failing after round after round of fixes.

Anything quoted back to the operator about a command goes through
:func:`ctrlrelay.web.transcript.summarize_command`, which builds it only
from fixed word lists, because the reason ends up in an issue comment.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ctrlrelay.core.obs import get_logger

_logger = get_logger("core.stuck")

# Calls in a row, or errors in a row, that count as going in circles.
DEFAULT_REPEAT_LIMIT = 5
# CI rounds in a row with the same failing tests that count as stuck. With
# the default of three fix attempts this stops the run before the last one:
# the first failure plus two fixes that changed nothing.
DEFAULT_SAME_FAILURE_ROUNDS = 3
# Times the relay re-runs a CI job that failed on infrastructure before it
# stops treating the failure as flaky.
DEFAULT_INFRA_RETRIES = 1

_POLL_SECONDS = 30.0
_TAIL_BYTES = 1_048_576


@dataclass(frozen=True)
class StuckPolicy:
    """How the live-transcript watch decides a run is stuck."""

    repeat_limit: int = DEFAULT_REPEAT_LIMIT
    poll_seconds: float = _POLL_SECONDS
    tail_bytes: int = _TAIL_BYTES


# --- the live transcript ------------------------------------------------------


@dataclass
class _Call:
    key: str
    name: str
    tool_input: dict[str, Any]
    ok: bool | None = None  # None until its result row is read
    error: str | None = None
    # The result's fingerprint, error or not; None until it is read.
    result: str | None = None


def _epoch(timestamp: Any) -> float | None:
    if not isinstance(timestamp, str):
        return None
    try:
        return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


# What changes between two runs of the same failing command without
# meaning anything changed: durations, memory addresses, temporary paths.
_NOISE = (
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|sec|seconds)\b"), "<t>"),
    (re.compile(r"0x[0-9a-fA-F]+"), "<addr>"),
    (re.compile(r"/tmp/\S+"), "<tmp>"),
    (re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?Z?"), "<ts>"),
)


def _fingerprint(text: str) -> str:
    for pattern, repl in _NOISE:
        text = pattern.sub(repl, text)
    return hashlib.sha256(text.strip().encode("utf-8", "replace")).hexdigest()


def _calls(lines: Iterable[str], since: float | None) -> list[_Call]:
    """Tool calls in transcript order, each with its result once read.

    Rows stamped before ``since`` are left out, so a resumed session is not
    judged on what an earlier session in the same worktree did.
    """
    calls: list[_Call] = []
    by_id: dict[str, _Call] = {}
    for line in lines:
        try:
            row = json.loads(line)
        except Exception:
            continue
        if not isinstance(row, dict):
            continue
        if since is not None:
            ts = _epoch(row.get("timestamp"))
            if ts is None or ts < since:
                continue
        message = row.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            continue
        for item in content:
            if not isinstance(item, dict):
                continue
            if row.get("type") == "assistant" and item.get("type") == "tool_use":
                name = item.get("name")
                tool_input = item.get("input")
                if not isinstance(name, str):
                    continue
                tool_input = tool_input if isinstance(tool_input, dict) else {}
                call = _Call(
                    key=name + json.dumps(tool_input, sort_keys=True, default=str),
                    name=name,
                    tool_input=tool_input,
                )
                calls.append(call)
                if isinstance(item.get("id"), str):
                    by_id[item["id"]] = call
            elif row.get("type") == "user" and item.get("type") == "tool_result":
                call = by_id.get(item.get("tool_use_id"))
                if call is None:
                    continue
                fingerprint = _fingerprint(_result_text(item.get("content")))
                call.result = fingerprint
                if item.get("is_error") is True:
                    call.ok = False
                    call.error = fingerprint
                else:
                    call.ok = True
    return calls


def _describe(call: _Call) -> str:
    """The call as it may be shown to anyone: fixed words only."""
    from ctrlrelay.web.transcript import summarize_command

    if call.name == "Bash":
        command = call.tool_input.get("command")
        if isinstance(command, str):
            try:
                return f"`{summarize_command(command)}`"
            except Exception:
                return "a shell command"
        return "a shell command"
    return f"the {call.name} tool with the same input"


def detect_loop(
    lines: Iterable[str],
    *,
    since: float | None = None,
    limit: int = DEFAULT_REPEAT_LIMIT,
) -> str | None:
    """Why the transcript shows the agent going in circles, or None.

    Two patterns, both in a row:

    - the last ``limit`` tool calls are the same call with the same input
      **and got the same result back** every time. An agent polling a
      background job runs one call over and over while the output moves
      on, and that is waiting, not a loop; a call whose result has not
      been read yet does not count;
    - the last ``limit`` failed calls are the same call failing with the
      same error, and that call did not succeed anywhere in between. Other
      calls in between (edits, reads) are allowed: an agent that edits,
      reruns and gets the identical error back five times is not fixing it.
    """
    if limit < 2:
        return None
    calls = _calls(lines, since)
    if len(calls) >= limit:
        tail = calls[-limit:]
        if (
            len({c.key for c in tail}) == 1
            and all(c.result is not None for c in tail)
            and len({c.result for c in tail}) == 1
        ):
            return (
                f"ran the same command {limit} times in a row: "
                f"{_describe(tail[-1])}"
            )

    failed = [i for i, c in enumerate(calls) if c.ok is False]
    if len(failed) >= limit:
        window = [calls[i] for i in failed[-limit:]]
        first = window[0]
        same = all(c.key == first.key and c.error == first.error for c in window)
        if same:
            start = failed[-limit]
            succeeded_between = any(
                c.key == first.key and c.ok is True for c in calls[start:]
            )
            if not succeeded_between:
                return (
                    f"got the same error {limit} times in a row from "
                    f"{_describe(first)}"
                )
    return None


def _tail_lines(path: Path, tail_bytes: int) -> list[str]:
    size = path.stat().st_size
    count = max(0, min(tail_bytes, size))
    with open(path, "rb") as fh:
        fh.seek(size - count)
        data = fh.read(count)
    lines = data.decode("utf-8", errors="replace").split("\n")
    if count < size:
        lines = lines[1:]  # the first is the end of a row cut in half
    return lines


def read_transcript(folder: Path, *, since: float, tail_bytes: int) -> list[str]:
    """The tail of the newest transcript in ``folder`` written since ``since``.

    Empty when there is none yet: an agent that has not written a row has
    not repeated anything.
    """
    try:
        candidates = [
            p for p in folder.glob("*.jsonl")
            if p.is_file() and p.stat().st_mtime >= since
        ]
    except OSError:
        return []
    if not candidates:
        return []
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    return _tail_lines(newest, tail_bytes)


async def watch_transcript(
    folders: list[Path],
    *,
    since: float,
    policy: StuckPolicy,
    ident: dict[str, Any] | None = None,
) -> str:
    """Poll the agent's transcript until it shows a loop; return the reason.

    Never returns otherwise: the caller cancels it when the agent exits. A
    read that fails is logged and the next poll tries again, so a transcript
    it cannot read never stops a run.
    """
    while True:
        await asyncio.sleep(policy.poll_seconds)
        for folder in folders:
            try:
                lines = read_transcript(
                    folder, since=since, tail_bytes=policy.tail_bytes
                )
                reason = detect_loop(lines, since=since, limit=policy.repeat_limit)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - must never stop a run
                _logger.warning(
                    "stuck.transcript_unreadable",
                    extra={
                        **(ident or {}),
                        "folder": str(folder),
                        "error_type": type(e).__name__,
                    },
                )
                continue
            if reason is not None:
                return reason


# --- CI between fix rounds ----------------------------------------------------


class FailureKind(StrEnum):
    INFRA = "flaky infrastructure"
    CODE = "code failure"
    BUDGET = "out of budget"


# A job that ran out of the time or money it was given. Re-running it
# spends the same budget again for the same result.
_BUDGET = re.compile(
    r"exceeded the maximum execution time"
    r"|has exceeded the maximum execution time"
    r"|spending limit"
    r"|recent account payments have failed"
    r"|you've used \d+% of included services",
    re.IGNORECASE,
)

# Failures that are about the machine or the network, not the code.
_INFRA = re.compile(
    r"runner has received a shutdown signal"
    r"|lost communication with the server"
    r"|the hosted runner encountered an error"
    r"|no space left on device"
    r"|could not resolve host"
    r"|temporary failure in name resolution"
    r"|connection reset by peer"
    r"|econnreset|etimedout|eai_again"
    r"|tls handshake timeout"
    r"|502 bad gateway|503 service unavailable|504 gateway time-?out"
    r"|toomanyrequests|api rate limit exceeded"
    r"|failed to download action"
    r"|unable to access 'https?://"
    r"|read timed out",
    re.IGNORECASE,
)

_TEST_PATTERNS = (
    # pytest summary lines: "FAILED tests/x.py::test_y - AssertionError"
    re.compile(r"\b(?:FAILED|ERROR)\s+(\S+\.py(?:::\S+)?)"),
    # go test
    re.compile(r"--- FAIL:\s+(\S+)"),
    # cargo test
    re.compile(r"\btest\s+(\S+)\s+\.\.\.\s+FAILED"),
    # jest / vitest
    re.compile(r"[✕×]\s+(.+?)(?:\s+\(\d+\s*m?s\))?\s*$", re.MULTILINE),
)


def failed_tests(log: str) -> frozenset[str]:
    """The names of the tests a CI log reports failing. Empty when it names none."""
    names: set[str] = set()
    for pattern in _TEST_PATTERNS:
        for match in pattern.finditer(log):
            names.add(match.group(1).strip())
    return frozenset(names)


def classify_failure(log: str) -> FailureKind:
    """What kind of failure a CI log shows.

    Out of budget wins, because nothing retried inside the same budget can
    finish. A log that names failing tests is a code failure even if it
    also mentions the network, since tests that hit the network can fail on
    it for code reasons. Infrastructure needs an infrastructure pattern and
    no failing test. Anything else is treated as a code failure: an unknown
    failure gets the agent's attention, never a blind re-run.
    """
    if _BUDGET.search(log):
        return FailureKind.BUDGET
    if failed_tests(log):
        return FailureKind.CODE
    if _INFRA.search(log):
        return FailureKind.INFRA
    return FailureKind.CODE


def classify_failures(
    logs: Iterable[str | None],
) -> tuple[FailureKind, frozenset[str]]:
    """The kind of a CI round with several failing jobs, and the failing
    tests they name. One entry per failing check: its own log, or ``None``
    when the relay could not read one.

    Each job is sorted on its own log, so one job's network error cannot
    make another job's failing tests read as flaky infrastructure, and the
    relay's own "could not fetch the log" text is never read as a CI log.
    Out of budget anywhere wins; infrastructure needs every job to be
    infrastructure; a job with no log is unknown, which is a code failure.
    """
    kinds: list[FailureKind] = []
    names: set[str] = set()
    for log in logs:
        if log is None:
            kinds.append(FailureKind.CODE)
            continue
        kind = classify_failure(log)
        kinds.append(kind)
        if kind is FailureKind.CODE:
            names |= failed_tests(log)
    if not kinds:
        return FailureKind.CODE, frozenset()
    if FailureKind.BUDGET in kinds:
        return FailureKind.BUDGET, frozenset()
    if all(k is FailureKind.INFRA for k in kinds):
        return FailureKind.INFRA, frozenset()
    return FailureKind.CODE, frozenset(names)


def format_tests(names: Iterable[str], limit: int = 5) -> str:
    ordered = sorted(names)
    shown = ", ".join(ordered[:limit])
    more = len(ordered) - limit
    return shown + (f" and {more} more" if more > 0 else "")
