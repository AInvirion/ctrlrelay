"""Checkpoint protocol for skill-orchestrator communication."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, model_validator


class CheckpointStatus(str, Enum):
    """Status values for checkpoint state."""

    DONE = "DONE"
    BLOCKED_NEEDS_INPUT = "BLOCKED_NEEDS_INPUT"
    FAILED = "FAILED"


class CheckpointState(BaseModel):
    """State written by skills to communicate with orchestrator."""

    version: str = "1"
    status: CheckpointStatus
    session_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    summary: str | None = None

    question: str | None = None
    question_context: dict[str, Any] | None = None

    error: str | None = None
    recoverable: bool = True

    outputs: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_status_fields(self) -> "CheckpointState":
        """Validate that status-dependent fields are present."""
        if self.status == CheckpointStatus.BLOCKED_NEEDS_INPUT and not self.question:
            raise ValueError("question is required when status is BLOCKED_NEEDS_INPUT")
        if self.status == CheckpointStatus.FAILED and not self.error:
            raise ValueError("error is required when status is FAILED")
        return self


class CheckpointError(Exception):
    """Raised when checkpoint operations fail."""


def _get_state_file() -> Path:
    """Get state file path from environment."""
    path = os.environ.get("CTRLRELAY_STATE_FILE")
    if not path:
        raise CheckpointError("CTRLRELAY_STATE_FILE environment variable not set")
    return Path(path)


def _get_session_id() -> str:
    """Get session ID from environment."""
    session_id = os.environ.get("CTRLRELAY_SESSION_ID")
    if not session_id:
        raise CheckpointError("CTRLRELAY_SESSION_ID environment variable not set")
    return session_id


def _write_checkpoint(state: CheckpointState) -> None:
    """Write checkpoint state atomically."""
    state_file = _get_state_file()
    temp_file = state_file.with_suffix(".json.tmp")

    state_file.parent.mkdir(parents=True, exist_ok=True)

    # Write to temp file first
    temp_file.write_text(state.model_dump_json(indent=2))

    # Atomic rename
    temp_file.rename(state_file)


def done(summary: str, outputs: dict[str, Any] | None = None) -> None:
    """Report successful completion."""
    # Check state file first so error message is consistent
    _get_state_file()
    state = CheckpointState(
        status=CheckpointStatus.DONE,
        session_id=_get_session_id(),
        summary=summary,
        outputs=outputs or {},
    )
    _write_checkpoint(state)


def blocked(question: str, context: dict[str, Any] | None = None) -> None:
    """Report blocked on human input."""
    # Check state file first so error message is consistent
    _get_state_file()
    state = CheckpointState(
        status=CheckpointStatus.BLOCKED_NEEDS_INPUT,
        session_id=_get_session_id(),
        question=question,
        question_context=context,
    )
    _write_checkpoint(state)


def failed(error: str, recoverable: bool = True) -> None:
    """Report failure."""
    # Check state file first so error message is consistent
    _get_state_file()
    state = CheckpointState(
        status=CheckpointStatus.FAILED,
        session_id=_get_session_id(),
        error=error,
        recoverable=recoverable,
    )
    _write_checkpoint(state)


# A checkpoint is a few hundred bytes; a DONE with a long pull-request
# description is a few kilobytes. Anything past this is not a checkpoint,
# and reading it whole is how an agent would exhaust the relay's memory.
# What an agent is told about free text it puts in a checkpoint written
# by :func:`shell_checkpoint`. The text sits on its own lines inside a
# Python raw triple-quoted string in a quoted heredoc, so quotes,
# backslashes, `$`, backticks and newlines all arrive as written (a
# carriage return arrives as a newline). Two things end it early: three
# single quotes in a row close the string, and a line that is exactly the
# heredoc's delimiter closes the heredoc.
CHECKPOINT_TEXT_RULE = (
    "Put your text on the line between the triple quotes. It may span "
    "lines and contain any quotes, but not three single quotes in a row "
    "and no line that is exactly CTRLRELAY_STATE."
)


def shell_checkpoint(
    quoted_dir: str,
    quoted_path: str,
    *,
    session_id: str,
    status: str,
    field: str,
    text: str,
) -> str:
    """The shell an agent runs to write a one-text-field checkpoint (#397).

    ``quoted_dir`` and ``quoted_path`` must already be shell-quoted
    (#191). ``text`` is a placeholder such as ``<QUESTION>`` for the agent
    to replace, or fixed text when the caller knows it.

    The earlier snippet was a ``printf`` whose format string was several
    single-quoted pieces joined by backslash continuations. Agents
    re-quoted it (``'…''…'``) or dropped a continuation, and the round
    ended with no checkpoint after the work was done. It also could not
    carry a question holding a double quote or a newline: ``%s`` pasted it
    into the JSON unescaped. Here the shell sees one plain command and a
    heredoc whose delimiter is quoted, so nothing is expanded and nothing
    needs re-quoting, and ``json.dump`` does the escaping.

    The text goes on its own lines and is stripped, so a backslash at its
    end cannot escape the closing quotes.
    """
    return f"""mkdir -p -- {quoted_dir}
python3 - {quoted_path} <<'CTRLRELAY_STATE'
import datetime, json, sys
text = r'''
{text}
'''.strip()
state = {{
    "version": "1",
    "status": "{status}",
    "session_id": "{session_id}",
    "timestamp": datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    ),
    "{field}": text,
}}
with open(sys.argv[1], "w") as f:
    json.dump(state, f)
CTRLRELAY_STATE"""


MAX_CHECKPOINT_BYTES = 256 * 1024


def read_checkpoint(path: Path, delete_after: bool = False) -> CheckpointState:
    """Read and parse a checkpoint file.

    Used by the orchestrator to read skill results. The agent controls the
    state path, so only a small regular file is read (#394): it is opened
    with ``O_NOFOLLOW`` (a symlink is refused, not followed) and
    ``O_NONBLOCK`` (a FIFO opens at once instead of waiting for a writer
    that never comes), checked with ``fstat`` on that same descriptor, and
    read up to :data:`MAX_CHECKPOINT_BYTES`. Before this a FIFO there
    blocked the read forever — after the session timeout had stopped
    applying — and stalled every other run on the event loop with it.

    Args:
        path: Path to the checkpoint file.
        delete_after: If True, delete the file after reading. A file that
            was refused is left where it is.

    Returns:
        Parsed CheckpointState.

    Raises:
        CheckpointError: If the file is missing, is not a small regular
            file, or does not parse as a checkpoint.
    """
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    # The directory too: the agent owns `.ctrlrelay`, and a symlink there to
    # another run's would hand this run that run's result — and the delete
    # below would remove it. Everything above it is the relay's.
    try:
        dir_fd = os.open(
            path.parent, os.O_RDONLY | os.O_DIRECTORY | nofollow | cloexec,
        )
    except FileNotFoundError as e:
        raise CheckpointError(f"Checkpoint file not found: {path}") from e
    except OSError as e:
        raise CheckpointError(
            f"Checkpoint directory could not be opened as a directory: {e}"
        ) from e
    try:
        return _read_checkpoint_at(dir_fd, path, delete_after)
    finally:
        os.close(dir_fd)


def _read_checkpoint_at(
    dir_fd: int, path: Path, delete_after: bool,
) -> CheckpointState:
    """:func:`read_checkpoint` once its directory is open as ``dir_fd``."""
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path.name, flags, dir_fd=dir_fd)
    except FileNotFoundError as e:
        raise CheckpointError(f"Checkpoint file not found: {path}") from e
    except OSError as e:
        # ELOOP is how O_NOFOLLOW says "that was a symlink".
        raise CheckpointError(
            f"Checkpoint file could not be opened as a regular file: {e}"
        ) from e
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise CheckpointError(
                f"Checkpoint is not a regular file: {path}"
            )
        if st.st_size > MAX_CHECKPOINT_BYTES:
            raise CheckpointError(
                f"Checkpoint is larger than {MAX_CHECKPOINT_BYTES} bytes: {path}"
            )
        chunks: list[bytes] = []
        remaining = MAX_CHECKPOINT_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as e:
        raise CheckpointError(f"Failed to read checkpoint file: {e}") from e
    finally:
        os.close(fd)
    raw = b"".join(chunks)
    if len(raw) > MAX_CHECKPOINT_BYTES:
        # Grew between the fstat and the read.
        raise CheckpointError(
            f"Checkpoint is larger than {MAX_CHECKPOINT_BYTES} bytes: {path}"
        )

    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as e:
        # `ValueError` covers JSONDecodeError, UnicodeDecodeError and an
        # integer past the int-string limit; `RecursionError` deep nesting.
        raise CheckpointError(f"Failed to parse checkpoint file: {e}") from e

    try:
        state = CheckpointState.model_validate(data)
    except Exception as e:
        raise CheckpointError(f"Invalid checkpoint data: {e}") from e

    if delete_after:
        # Only the file that was read: if the name was swapped since, the
        # new entry is not this function's to remove.
        try:
            now = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
        except FileNotFoundError:
            return state
        if (now.st_dev, now.st_ino) == (st.st_dev, st.st_ino):
            os.unlink(path.name, dir_fd=dir_fd)

    return state
