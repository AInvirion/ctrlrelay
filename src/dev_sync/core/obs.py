"""Structured logging for dev-sync.

Emits newline-delimited JSON records on the ``dev_sync`` logger tree so the
poller and bridge stdout streams (captured by launchd into
``~/.dev-sync/logs/*.log``) carry machine-readable events.

Typical usage:

    from dev_sync.core.obs import get_logger, log_event

    logger = get_logger("transport.socket")
    log_event(
        logger,
        "dev.question.posted",
        session_id=session_id,
        repo=repo,
        issue_number=issue_number,
        transport="socket",
    )
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
from typing import Any

_CONFIGURED = False

# LogRecord attributes that should not be serialized as "extra" fields.
_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JSONFormatter(logging.Formatter):
    """Format records as single-line JSON with ``event`` + extra fields."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install a JSON handler on the ``dev_sync`` logger tree.

    Safe to call repeatedly — subsequent calls are no-ops.  Records propagate
    to the root logger so pytest's ``caplog`` fixture (and any other ancestor
    handlers) can still observe them.
    """
    global _CONFIGURED
    if _CONFIGURED:
        return
    logger = logging.getLogger("dev_sync")
    logger.setLevel(level)
    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return a namespaced logger under ``dev_sync.<name>``."""
    configure_logging()
    return logging.getLogger(f"dev_sync.{name}")


def log_event(logger: logging.Logger, event: str, **fields: Any) -> None:
    """Emit an INFO-level structured event.

    ``fields`` are attached as LogRecord attributes and surfaced by
    :class:`JSONFormatter`.  Avoid using keys that collide with stdlib
    LogRecord attributes (``message``, ``name``, ``args`` …) — those are
    reserved by the logging module and will raise ``KeyError``.
    """
    logger.info(event, extra=fields)


def hash_text(text: str) -> str:
    """Short, stable SHA-256 prefix for PII-sensitive payloads."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
