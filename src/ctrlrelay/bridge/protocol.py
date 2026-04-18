"""Bridge protocol message types and serialization."""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel


class ProtocolError(Exception):
    """Raised when protocol parsing fails."""


class BridgeOp(str, Enum):
    """Bridge operation types."""

    SEND = "send"
    ASK = "ask"
    ACK = "ack"
    ANSWER = "answer"
    PING = "ping"
    PONG = "pong"
    ERROR = "error"


class BridgeMessage(BaseModel):
    """Message exchanged between orchestrator and bridge."""

    op: BridgeOp
    request_id: str | None = None

    # send
    text: str | None = None

    # ask
    question: str | None = None
    options: list[str] | None = None
    timeout: int | None = None

    # ack
    status: str | None = None

    # answer
    answer: str | None = None
    answered_at: str | None = None

    # error
    error: str | None = None
    message: str | None = None

    # Correlation fields (optional; used for structured logging only).
    session_id: str | None = None
    repo: str | None = None
    issue_number: int | None = None


def serialize_message(msg: BridgeMessage) -> str:
    """Serialize message to newline-delimited JSON."""
    data = msg.model_dump(exclude_none=True)
    return json.dumps(data) + "\n"


def parse_message(line: str) -> BridgeMessage:
    """Parse newline-delimited JSON to message."""
    try:
        data: dict[str, Any] = json.loads(line.strip())
    except json.JSONDecodeError as e:
        raise ProtocolError(f"Invalid JSON: {e}") from e

    if "op" not in data:
        raise ProtocolError("Missing 'op' field in message")

    return BridgeMessage.model_validate(data)
