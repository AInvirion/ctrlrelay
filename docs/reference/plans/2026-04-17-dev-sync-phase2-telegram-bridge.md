---
title: Phase 2 — Telegram Bridge
layout: default
parent: Plans
grand_parent: Design & history
nav_order: 3
---

# Telegram Bridge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enable human-in-the-loop communication via Telegram, with `dev-sync bridge test` delivering a message to the user's phone.

**Architecture:** Transport abstraction defines async Protocol for send/ask operations. TelegramTransport connects to a separate Bridge process via Unix socket. Bridge process handles Telegram Bot API and maintains message state.

**Tech Stack:** asyncio, python-telegram-bot, Unix sockets (newline-delimited JSON protocol)

---

## File Structure

```
src/dev_sync/
├── transports/
│   ├── __init__.py          # Export Transport, get_transport()
│   ├── base.py               # Transport Protocol definition
│   ├── file_mock.py          # FileMockTransport for testing
│   └── socket_client.py      # SocketTransport (connects to bridge)
├── bridge/
│   ├── __init__.py           # Export BridgeServer
│   ├── protocol.py           # Message types (BridgeOp, BridgeMessage)
│   ├── server.py             # Unix socket server
│   └── telegram_handler.py   # Telegram Bot API integration
└── cli.py                    # Add bridge_app subcommand group

tests/
├── test_transport.py         # Transport abstraction tests
├── test_bridge_protocol.py   # Protocol message tests
└── test_bridge_server.py     # Bridge server tests (mocked Telegram)
```

---

### Task 1: Add python-telegram-bot dependency

**Files:**
- Modify: `pyproject.toml`

- [ ] **Step 1: Add dependency**

Add to `pyproject.toml` dependencies list:
```toml
dependencies = [
    "typer>=0.12.0",
    "pydantic>=2.0.0",
    "pyyaml>=6.0.0",
    "rich>=13.0.0",
    "python-telegram-bot>=21.0",
]
```

- [ ] **Step 2: Install updated dependencies**

Run:
```bash
pip install -e ".[dev]"
```
Expected: Successfully installs python-telegram-bot

- [ ] **Step 3: Verify import works**

Run:
```bash
python -c "import telegram; print('telegram-bot:', telegram.__version__)"
```
Expected: Prints version number

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add python-telegram-bot dependency"
```

---

### Task 2: Create bridge protocol message types

**Files:**
- Create: `src/dev_sync/bridge/__init__.py`
- Create: `src/dev_sync/bridge/protocol.py`
- Create: `tests/test_bridge_protocol.py`

- [ ] **Step 1: Create bridge package init**

Create `src/dev_sync/bridge/__init__.py`:
```python
"""Bridge process for Telegram communication."""

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    parse_message,
    serialize_message,
)

__all__ = [
    "BridgeMessage",
    "BridgeOp",
    "parse_message",
    "serialize_message",
]
```

- [ ] **Step 2: Write failing tests for protocol**

Create `tests/test_bridge_protocol.py`:
```python
"""Tests for bridge protocol messages."""

import json

import pytest


class TestBridgeOp:
    def test_all_ops_defined(self) -> None:
        """All required operations should be defined."""
        from dev_sync.bridge.protocol import BridgeOp

        assert BridgeOp.SEND == "send"
        assert BridgeOp.ASK == "ask"
        assert BridgeOp.ACK == "ack"
        assert BridgeOp.ANSWER == "answer"
        assert BridgeOp.PING == "ping"
        assert BridgeOp.PONG == "pong"
        assert BridgeOp.ERROR == "error"


class TestBridgeMessage:
    def test_send_message(self) -> None:
        """Should create send message."""
        from dev_sync.bridge.protocol import BridgeMessage, BridgeOp

        msg = BridgeMessage(op=BridgeOp.SEND, request_id="r-001", text="Hello")
        assert msg.op == BridgeOp.SEND
        assert msg.request_id == "r-001"
        assert msg.text == "Hello"

    def test_ask_message_with_options(self) -> None:
        """Should create ask message with options."""
        from dev_sync.bridge.protocol import BridgeMessage, BridgeOp

        msg = BridgeMessage(
            op=BridgeOp.ASK,
            request_id="r-002",
            question="Approve?",
            options=["yes", "no"],
        )
        assert msg.question == "Approve?"
        assert msg.options == ["yes", "no"]

    def test_ack_message(self) -> None:
        """Should create ack message."""
        from dev_sync.bridge.protocol import BridgeMessage, BridgeOp

        msg = BridgeMessage(op=BridgeOp.ACK, request_id="r-001", status="sent")
        assert msg.status == "sent"

    def test_answer_message(self) -> None:
        """Should create answer message."""
        from dev_sync.bridge.protocol import BridgeMessage, BridgeOp

        msg = BridgeMessage(
            op=BridgeOp.ANSWER,
            request_id="r-002",
            answer="yes",
            answered_at="2026-04-17T12:00:00Z",
        )
        assert msg.answer == "yes"

    def test_error_message(self) -> None:
        """Should create error message."""
        from dev_sync.bridge.protocol import BridgeMessage, BridgeOp

        msg = BridgeMessage(
            op=BridgeOp.ERROR,
            request_id="r-003",
            error="telegram_api_error",
            message="Rate limited",
        )
        assert msg.error == "telegram_api_error"
        assert msg.message == "Rate limited"


class TestSerialize:
    def test_serialize_message(self) -> None:
        """Should serialize to JSON line."""
        from dev_sync.bridge.protocol import BridgeMessage, BridgeOp, serialize_message

        msg = BridgeMessage(op=BridgeOp.PING)
        line = serialize_message(msg)
        assert line.endswith("\n")
        data = json.loads(line)
        assert data["op"] == "ping"

    def test_parse_message(self) -> None:
        """Should parse JSON line to message."""
        from dev_sync.bridge.protocol import BridgeOp, parse_message

        line = '{"op": "pong"}\n'
        msg = parse_message(line)
        assert msg.op == BridgeOp.PONG

    def test_parse_invalid_json_raises(self) -> None:
        """Should raise on invalid JSON."""
        from dev_sync.bridge.protocol import ProtocolError, parse_message

        with pytest.raises(ProtocolError, match="Invalid JSON"):
            parse_message("not json\n")

    def test_parse_missing_op_raises(self) -> None:
        """Should raise if op field missing."""
        from dev_sync.bridge.protocol import ProtocolError, parse_message

        with pytest.raises(ProtocolError, match="Missing 'op'"):
            parse_message('{"request_id": "r-001"}\n')
```

- [ ] **Step 3: Run tests to verify they fail**

Run:
```bash
pytest tests/test_bridge_protocol.py -v
```
Expected: FAIL with "No module named 'dev_sync.bridge'"

- [ ] **Step 4: Implement protocol module**

Create `src/dev_sync/bridge/protocol.py`:
```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
pytest tests/test_bridge_protocol.py -v
```
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/bridge/ tests/test_bridge_protocol.py
git commit -m "feat: add bridge protocol message types"
```

---

### Task 3: Create transport abstraction

**Files:**
- Create: `src/dev_sync/transports/__init__.py`
- Create: `src/dev_sync/transports/base.py`
- Create: `tests/test_transport.py`

- [ ] **Step 1: Create transports package init**

Create `src/dev_sync/transports/__init__.py`:
```python
"""Transport abstraction for orchestrator communication."""

from dev_sync.transports.base import Transport, TransportError

__all__ = [
    "Transport",
    "TransportError",
]
```

- [ ] **Step 2: Write failing tests for transport protocol**

Create `tests/test_transport.py`:
```python
"""Tests for transport abstraction."""

from typing import Protocol, runtime_checkable

import pytest


class TestTransportProtocol:
    def test_transport_is_protocol(self) -> None:
        """Transport should be a Protocol."""
        from dev_sync.transports.base import Transport

        assert hasattr(Transport, "__protocol_attrs__") or isinstance(
            Transport, type
        )

    def test_transport_has_send(self) -> None:
        """Transport should define send method."""
        from dev_sync.transports.base import Transport

        assert hasattr(Transport, "send")

    def test_transport_has_ask(self) -> None:
        """Transport should define ask method."""
        from dev_sync.transports.base import Transport

        assert hasattr(Transport, "ask")

    def test_transport_has_close(self) -> None:
        """Transport should define close method."""
        from dev_sync.transports.base import Transport

        assert hasattr(Transport, "close")


class TestTransportError:
    def test_error_exists(self) -> None:
        """TransportError should be defined."""
        from dev_sync.transports.base import TransportError

        assert issubclass(TransportError, Exception)
```

- [ ] **Step 3: Run tests to verify they fail**

Run:
```bash
pytest tests/test_transport.py -v
```
Expected: FAIL with "No module named 'dev_sync.transports'"

- [ ] **Step 4: Implement transport base**

Create `src/dev_sync/transports/base.py`:
```python
"""Transport protocol definition."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


class TransportError(Exception):
    """Raised when transport operations fail."""


@runtime_checkable
class Transport(Protocol):
    """Protocol for orchestrator-to-human communication."""

    async def send(self, message: str) -> None:
        """Send a one-way message (no response expected)."""
        ...

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: int = 300,
    ) -> str:
        """Ask a question and wait for response."""
        ...

    async def close(self) -> None:
        """Close the transport connection."""
        ...
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
pytest tests/test_transport.py -v
```
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/transports/ tests/test_transport.py
git commit -m "feat: add transport protocol abstraction"
```

---

### Task 4: Implement FileMockTransport

**Files:**
- Create: `src/dev_sync/transports/file_mock.py`
- Modify: `src/dev_sync/transports/__init__.py`
- Modify: `tests/test_transport.py`

- [ ] **Step 1: Write failing tests for FileMockTransport**

Add to `tests/test_transport.py`:
```python
class TestFileMockTransport:
    @pytest.fixture
    def mock_files(self, tmp_path):
        """Create inbox/outbox files."""
        inbox = tmp_path / "inbox.txt"
        outbox = tmp_path / "outbox.txt"
        inbox.touch()
        outbox.touch()
        return inbox, outbox

    @pytest.mark.asyncio
    async def test_send_writes_to_outbox(self, mock_files) -> None:
        """send() should write message to outbox."""
        from dev_sync.transports.file_mock import FileMockTransport

        inbox, outbox = mock_files
        transport = FileMockTransport(inbox=inbox, outbox=outbox)

        await transport.send("Hello world")

        content = outbox.read_text()
        assert "Hello world" in content

    @pytest.mark.asyncio
    async def test_ask_writes_question_reads_answer(self, mock_files) -> None:
        """ask() should write question and read answer from inbox."""
        from dev_sync.transports.file_mock import FileMockTransport

        inbox, outbox = mock_files
        inbox.write_text("yes\n")

        transport = FileMockTransport(inbox=inbox, outbox=outbox)
        answer = await transport.ask("Approve?", options=["yes", "no"])

        assert answer == "yes"
        assert "Approve?" in outbox.read_text()

    @pytest.mark.asyncio
    async def test_ask_timeout_raises(self, mock_files) -> None:
        """ask() should raise on timeout with empty inbox."""
        from dev_sync.transports.base import TransportError
        from dev_sync.transports.file_mock import FileMockTransport

        inbox, outbox = mock_files
        transport = FileMockTransport(inbox=inbox, outbox=outbox)

        with pytest.raises(TransportError, match="timeout"):
            await transport.ask("Question?", timeout=1)

    @pytest.mark.asyncio
    async def test_implements_protocol(self, mock_files) -> None:
        """FileMockTransport should implement Transport protocol."""
        from dev_sync.transports.base import Transport
        from dev_sync.transports.file_mock import FileMockTransport

        inbox, outbox = mock_files
        transport = FileMockTransport(inbox=inbox, outbox=outbox)
        assert isinstance(transport, Transport)
```

- [ ] **Step 2: Add pytest-asyncio dependency**

Add to `pyproject.toml` dev dependencies:
```toml
[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-cov>=4.0.0",
    "pytest-asyncio>=0.23.0",
    "ruff>=0.4.0",
]
```

Run:
```bash
pip install -e ".[dev]"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:
```bash
pytest tests/test_transport.py::TestFileMockTransport -v
```
Expected: FAIL with "No module named 'dev_sync.transports.file_mock'"

- [ ] **Step 4: Implement FileMockTransport**

Create `src/dev_sync/transports/file_mock.py`:
```python
"""File-based mock transport for testing."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

from dev_sync.transports.base import TransportError


class FileMockTransport:
    """Transport that reads/writes to files for testing."""

    def __init__(self, inbox: Path, outbox: Path) -> None:
        self.inbox = inbox
        self.outbox = outbox

    async def send(self, message: str) -> None:
        """Write message to outbox file."""
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.outbox.open("a") as f:
            f.write(f"[{timestamp}] {message}\n")

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: int = 300,
    ) -> str:
        """Write question to outbox and poll inbox for answer."""
        # Write question
        timestamp = datetime.now(timezone.utc).isoformat()
        opts = f" [{'/'.join(options)}]" if options else ""
        with self.outbox.open("a") as f:
            f.write(f"[{timestamp}] QUESTION: {question}{opts}\n")

        # Poll inbox for answer
        start = asyncio.get_event_loop().time()
        while True:
            content = self.inbox.read_text().strip()
            if content:
                # Clear inbox after reading
                self.inbox.write_text("")
                return content.split("\n")[0].strip()

            if asyncio.get_event_loop().time() - start > timeout:
                raise TransportError(f"ask() timeout after {timeout}s")

            await asyncio.sleep(0.5)

    async def close(self) -> None:
        """No-op for file transport."""
        pass
```

- [ ] **Step 5: Update transports __init__.py**

Update `src/dev_sync/transports/__init__.py`:
```python
"""Transport abstraction for orchestrator communication."""

from dev_sync.transports.base import Transport, TransportError
from dev_sync.transports.file_mock import FileMockTransport

__all__ = [
    "FileMockTransport",
    "Transport",
    "TransportError",
]
```

- [ ] **Step 6: Run tests to verify they pass**

Run:
```bash
pytest tests/test_transport.py -v
```
Expected: All tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/dev_sync/transports/ tests/test_transport.py pyproject.toml
git commit -m "feat: add FileMockTransport for testing"
```

---

### Task 5: Implement SocketTransport (bridge client)

**Files:**
- Create: `src/dev_sync/transports/socket_client.py`
- Modify: `src/dev_sync/transports/__init__.py`
- Modify: `tests/test_transport.py`

- [ ] **Step 1: Write failing tests for SocketTransport**

Add to `tests/test_transport.py`:
```python
import asyncio
import tempfile


class TestSocketTransport:
    @pytest.fixture
    def socket_path(self, tmp_path):
        """Create temp socket path."""
        return tmp_path / "test.sock"

    @pytest.mark.asyncio
    async def test_connect_fails_when_no_server(self, socket_path) -> None:
        """Should raise when bridge not running."""
        from dev_sync.transports.base import TransportError
        from dev_sync.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path)
        with pytest.raises(TransportError, match="connect"):
            await transport.connect()

    @pytest.mark.asyncio
    async def test_send_requires_connection(self, socket_path) -> None:
        """Should raise if not connected."""
        from dev_sync.transports.base import TransportError
        from dev_sync.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path)
        with pytest.raises(TransportError, match="not connected"):
            await transport.send("test")

    @pytest.mark.asyncio
    async def test_implements_protocol(self, socket_path) -> None:
        """SocketTransport should implement Transport protocol."""
        from dev_sync.transports.base import Transport
        from dev_sync.transports.socket_client import SocketTransport

        transport = SocketTransport(socket_path)
        assert isinstance(transport, Transport)
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_transport.py::TestSocketTransport -v
```
Expected: FAIL with "No module named 'dev_sync.transports.socket_client'"

- [ ] **Step 3: Implement SocketTransport**

Create `src/dev_sync/transports/socket_client.py`:
```python
"""Unix socket transport client for bridge communication."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from dev_sync.transports.base import TransportError


class SocketTransport:
    """Transport that connects to bridge via Unix socket."""

    def __init__(self, socket_path: Path) -> None:
        self.socket_path = socket_path
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pending: dict[str, asyncio.Future[BridgeMessage]] = {}
        self._receive_task: asyncio.Task | None = None

    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        """Connect to bridge socket."""
        try:
            self._reader, self._writer = await asyncio.open_unix_connection(
                str(self.socket_path)
            )
            self._receive_task = asyncio.create_task(self._receive_loop())
        except (OSError, ConnectionRefusedError) as e:
            raise TransportError(f"Failed to connect to bridge: {e}") from e

    async def _receive_loop(self) -> None:
        """Background task to receive messages from bridge."""
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                try:
                    msg = parse_message(line.decode())
                    if msg.request_id and msg.request_id in self._pending:
                        self._pending[msg.request_id].set_result(msg)
                except ProtocolError:
                    pass
        except asyncio.CancelledError:
            pass

    async def _send_message(self, msg: BridgeMessage) -> None:
        """Send message to bridge."""
        if not self.connected:
            raise TransportError("Transport not connected")
        assert self._writer is not None
        data = serialize_message(msg).encode()
        self._writer.write(data)
        await self._writer.drain()

    async def _send_and_wait(
        self, msg: BridgeMessage, timeout: int
    ) -> BridgeMessage:
        """Send message and wait for response."""
        assert msg.request_id is not None
        future: asyncio.Future[BridgeMessage] = asyncio.get_event_loop().create_future()
        self._pending[msg.request_id] = future

        try:
            await self._send_message(msg)
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TransportError(f"Timeout waiting for response") from e
        finally:
            self._pending.pop(msg.request_id, None)

    async def send(self, message: str) -> None:
        """Send a one-way message."""
        request_id = f"r-{uuid.uuid4().hex[:8]}"
        msg = BridgeMessage(op=BridgeOp.SEND, request_id=request_id, text=message)
        await self._send_message(msg)

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
        timeout: int = 300,
    ) -> str:
        """Ask a question and wait for response."""
        request_id = f"r-{uuid.uuid4().hex[:8]}"
        msg = BridgeMessage(
            op=BridgeOp.ASK,
            request_id=request_id,
            question=question,
            options=options,
            timeout=timeout,
        )
        response = await self._send_and_wait(msg, timeout)

        if response.op == BridgeOp.ERROR:
            raise TransportError(f"Bridge error: {response.message}")
        if response.op == BridgeOp.ANSWER and response.answer:
            return response.answer

        raise TransportError(f"Unexpected response: {response.op}")

    async def close(self) -> None:
        """Close the connection."""
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._writer:
            self._writer.close()
            await self._writer.wait_closed()
        self._reader = None
        self._writer = None
```

- [ ] **Step 4: Update transports __init__.py**

Update `src/dev_sync/transports/__init__.py`:
```python
"""Transport abstraction for orchestrator communication."""

from dev_sync.transports.base import Transport, TransportError
from dev_sync.transports.file_mock import FileMockTransport
from dev_sync.transports.socket_client import SocketTransport

__all__ = [
    "FileMockTransport",
    "SocketTransport",
    "Transport",
    "TransportError",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
pytest tests/test_transport.py -v
```
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/transports/ tests/test_transport.py
git commit -m "feat: add SocketTransport for bridge communication"
```

---

### Task 6: Implement bridge server (socket + basic handlers)

**Files:**
- Create: `src/dev_sync/bridge/server.py`
- Modify: `src/dev_sync/bridge/__init__.py`
- Create: `tests/test_bridge_server.py`

- [ ] **Step 1: Write failing tests for bridge server**

Create `tests/test_bridge_server.py`:
```python
"""Tests for bridge server."""

import asyncio
import os
import stat

import pytest


class TestBridgeServer:
    @pytest.fixture
    def socket_path(self, tmp_path):
        return tmp_path / "bridge.sock"

    @pytest.mark.asyncio
    async def test_creates_socket_file(self, socket_path) -> None:
        """Server should create socket file."""
        from dev_sync.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        assert socket_path.exists()

        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_socket_permissions(self, socket_path) -> None:
        """Socket should have 0600 permissions."""
        from dev_sync.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        mode = stat.S_IMODE(os.stat(socket_path).st_mode)
        assert mode == 0o600

        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_handles_ping_pong(self, socket_path) -> None:
        """Server should respond to ping with pong."""
        from dev_sync.bridge.protocol import BridgeOp, parse_message, serialize_message, BridgeMessage
        from dev_sync.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        # Connect and send ping
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        ping = serialize_message(BridgeMessage(op=BridgeOp.PING))
        writer.write(ping.encode())
        await writer.drain()

        # Read pong
        response = await asyncio.wait_for(reader.readline(), timeout=1)
        msg = parse_message(response.decode())
        assert msg.op == BridgeOp.PONG

        writer.close()
        await writer.wait_closed()
        await server.stop()
        task.cancel()

    @pytest.mark.asyncio
    async def test_cleans_up_socket_on_stop(self, socket_path) -> None:
        """Server should remove socket file on stop."""
        from dev_sync.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)
        assert socket_path.exists()

        await server.stop()
        task.cancel()

        assert not socket_path.exists()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_bridge_server.py -v
```
Expected: FAIL with "cannot import name 'BridgeServer'"

- [ ] **Step 3: Implement BridgeServer**

Create `src/dev_sync/bridge/server.py`:
```python
"""Bridge server for Telegram communication."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)


class BridgeServer:
    """Unix socket server that bridges to Telegram."""

    def __init__(
        self,
        socket_path: Path,
        bot_token: str,
        chat_id: int,
    ) -> None:
        self.socket_path = socket_path
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._server: asyncio.Server | None = None
        self._running = False

    async def start(self) -> None:
        """Start the bridge server."""
        # Remove existing socket
        if self.socket_path.exists():
            self.socket_path.unlink()

        # Create parent directory
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Start server
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )

        # Set permissions to 0600
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)

        self._running = True
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the bridge server."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Clean up socket
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a client connection."""
        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break

                try:
                    msg = parse_message(line.decode())
                    response = await self._handle_message(msg)
                    if response:
                        writer.write(serialize_message(response).encode())
                        await writer.drain()
                except ProtocolError:
                    pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_message(self, msg: BridgeMessage) -> BridgeMessage | None:
        """Handle a single message and return response."""
        if msg.op == BridgeOp.PING:
            return BridgeMessage(op=BridgeOp.PONG)

        if msg.op == BridgeOp.SEND:
            # Will implement Telegram sending in next task
            return BridgeMessage(
                op=BridgeOp.ACK,
                request_id=msg.request_id,
                status="sent",
            )

        if msg.op == BridgeOp.ASK:
            # Will implement Telegram asking in next task
            return BridgeMessage(
                op=BridgeOp.ACK,
                request_id=msg.request_id,
                status="pending",
            )

        return None
```

- [ ] **Step 4: Update bridge __init__.py**

Update `src/dev_sync/bridge/__init__.py`:
```python
"""Bridge process for Telegram communication."""

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from dev_sync.bridge.server import BridgeServer

__all__ = [
    "BridgeMessage",
    "BridgeOp",
    "BridgeServer",
    "ProtocolError",
    "parse_message",
    "serialize_message",
]
```

- [ ] **Step 5: Run tests to verify they pass**

Run:
```bash
pytest tests/test_bridge_server.py -v
```
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add src/dev_sync/bridge/ tests/test_bridge_server.py
git commit -m "feat: add bridge server with socket handling"
```

---

### Task 7: Add Telegram handler

**Files:**
- Create: `src/dev_sync/bridge/telegram_handler.py`
- Modify: `src/dev_sync/bridge/server.py`
- Modify: `src/dev_sync/bridge/__init__.py`
- Create: `tests/test_telegram_handler.py`

- [ ] **Step 1: Write failing tests for TelegramHandler**

Create `tests/test_telegram_handler.py`:
```python
"""Tests for Telegram handler."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestTelegramHandler:
    @pytest.mark.asyncio
    async def test_send_message(self) -> None:
        """Should send message via Telegram API."""
        from dev_sync.bridge.telegram_handler import TelegramHandler

        with patch("dev_sync.bridge.telegram_handler.Bot") as MockBot:
            mock_bot = AsyncMock()
            MockBot.return_value = mock_bot

            handler = TelegramHandler(bot_token="test-token", chat_id=12345)
            await handler.send("Hello world")

            mock_bot.send_message.assert_called_once_with(
                chat_id=12345,
                text="Hello world",
            )

    @pytest.mark.asyncio
    async def test_ask_sends_with_keyboard(self) -> None:
        """Should send question with reply keyboard."""
        from dev_sync.bridge.telegram_handler import TelegramHandler

        with patch("dev_sync.bridge.telegram_handler.Bot") as MockBot:
            mock_bot = AsyncMock()
            mock_message = MagicMock()
            mock_message.message_id = 42
            mock_bot.send_message.return_value = mock_message
            MockBot.return_value = mock_bot

            handler = TelegramHandler(bot_token="test-token", chat_id=12345)
            msg_id = await handler.ask("Approve?", options=["yes", "no"])

            assert msg_id == 42
            mock_bot.send_message.assert_called_once()
            call_kwargs = mock_bot.send_message.call_args.kwargs
            assert "Approve?" in call_kwargs["text"]
            assert call_kwargs["reply_markup"] is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_telegram_handler.py -v
```
Expected: FAIL with "cannot import name 'TelegramHandler'"

- [ ] **Step 3: Implement TelegramHandler**

Create `src/dev_sync/bridge/telegram_handler.py`:
```python
"""Telegram Bot API handler."""

from __future__ import annotations

from telegram import Bot, ReplyKeyboardMarkup, ReplyKeyboardRemove


class TelegramHandler:
    """Handles Telegram Bot API communication."""

    def __init__(self, bot_token: str, chat_id: int) -> None:
        self.bot = Bot(token=bot_token)
        self.chat_id = chat_id

    async def send(self, text: str) -> int:
        """Send a message to the configured chat."""
        message = await self.bot.send_message(
            chat_id=self.chat_id,
            text=text,
        )
        return message.message_id

    async def ask(
        self,
        question: str,
        options: list[str] | None = None,
    ) -> int:
        """Send a question with optional reply keyboard."""
        reply_markup = None
        if options:
            keyboard = [[opt] for opt in options]
            reply_markup = ReplyKeyboardMarkup(
                keyboard,
                one_time_keyboard=True,
                resize_keyboard=True,
            )

        message = await self.bot.send_message(
            chat_id=self.chat_id,
            text=question,
            reply_markup=reply_markup or ReplyKeyboardRemove(),
        )
        return message.message_id

    async def close(self) -> None:
        """Close the bot session."""
        await self.bot.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_telegram_handler.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Integrate TelegramHandler into BridgeServer**

Update `src/dev_sync/bridge/server.py` - modify the `__init__` and `_handle_message` methods:
```python
"""Bridge server for Telegram communication."""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from dev_sync.bridge.telegram_handler import TelegramHandler


class BridgeServer:
    """Unix socket server that bridges to Telegram."""

    def __init__(
        self,
        socket_path: Path,
        bot_token: str,
        chat_id: int,
    ) -> None:
        self.socket_path = socket_path
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._server: asyncio.Server | None = None
        self._running = False
        self._telegram: TelegramHandler | None = None
        self._pending_questions: dict[str, int] = {}  # request_id -> message_id

    async def start(self) -> None:
        """Start the bridge server."""
        # Initialize Telegram handler
        self._telegram = TelegramHandler(
            bot_token=self.bot_token,
            chat_id=self.chat_id,
        )

        # Remove existing socket
        if self.socket_path.exists():
            self.socket_path.unlink()

        # Create parent directory
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)

        # Start server
        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )

        # Set permissions to 0600
        os.chmod(self.socket_path, stat.S_IRUSR | stat.S_IWUSR)

        self._running = True
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        """Stop the bridge server."""
        self._running = False
        if self._telegram:
            await self._telegram.close()

        if self._server:
            self._server.close()
            await self._server.wait_closed()

        # Clean up socket
        if self.socket_path.exists():
            self.socket_path.unlink()

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a client connection."""
        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break

                try:
                    msg = parse_message(line.decode())
                    response = await self._handle_message(msg)
                    if response:
                        writer.write(serialize_message(response).encode())
                        await writer.drain()
                except ProtocolError:
                    pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_message(self, msg: BridgeMessage) -> BridgeMessage | None:
        """Handle a single message and return response."""
        if msg.op == BridgeOp.PING:
            return BridgeMessage(op=BridgeOp.PONG)

        if msg.op == BridgeOp.SEND:
            try:
                assert self._telegram is not None
                await self._telegram.send(msg.text or "")
                return BridgeMessage(
                    op=BridgeOp.ACK,
                    request_id=msg.request_id,
                    status="sent",
                )
            except Exception as e:
                return BridgeMessage(
                    op=BridgeOp.ERROR,
                    request_id=msg.request_id,
                    error="telegram_api_error",
                    message=str(e),
                )

        if msg.op == BridgeOp.ASK:
            try:
                assert self._telegram is not None
                msg_id = await self._telegram.ask(
                    msg.question or "",
                    options=msg.options,
                )
                if msg.request_id:
                    self._pending_questions[msg.request_id] = msg_id
                return BridgeMessage(
                    op=BridgeOp.ACK,
                    request_id=msg.request_id,
                    status="pending",
                )
            except Exception as e:
                return BridgeMessage(
                    op=BridgeOp.ERROR,
                    request_id=msg.request_id,
                    error="telegram_api_error",
                    message=str(e),
                )

        return None
```

- [ ] **Step 6: Update bridge __init__.py**

Update `src/dev_sync/bridge/__init__.py`:
```python
"""Bridge process for Telegram communication."""

from dev_sync.bridge.protocol import (
    BridgeMessage,
    BridgeOp,
    ProtocolError,
    parse_message,
    serialize_message,
)
from dev_sync.bridge.server import BridgeServer
from dev_sync.bridge.telegram_handler import TelegramHandler

__all__ = [
    "BridgeMessage",
    "BridgeOp",
    "BridgeServer",
    "ProtocolError",
    "TelegramHandler",
    "parse_message",
    "serialize_message",
]
```

- [ ] **Step 7: Run all bridge tests**

Run:
```bash
pytest tests/test_bridge*.py tests/test_telegram*.py -v
```
Expected: All tests PASS

- [ ] **Step 8: Commit**

```bash
git add src/dev_sync/bridge/ tests/test_telegram_handler.py
git commit -m "feat: add Telegram handler and integrate with bridge server"
```

---

### Task 8: Add bridge CLI commands

**Files:**
- Modify: `src/dev_sync/cli.py`

- [ ] **Step 1: Add bridge subcommand group**

Add to `src/dev_sync/cli.py` after the skills_app section:
```python
# Bridge subcommand group
bridge_app = typer.Typer(help="Telegram bridge commands.")
app.add_typer(bridge_app, name="bridge")


def _get_socket_path(config_path: str) -> Path:
    """Get socket path from config."""
    try:
        config = load_config(config_path)
        if config.transport.telegram:
            return config.transport.telegram.socket_path.expanduser().resolve()
    except ConfigError:
        pass
    return Path("~/.dev-sync/dev-sync.sock").expanduser().resolve()


def _get_bridge_pid_file(socket_path: Path) -> Path:
    """Get PID file path for bridge process."""
    return socket_path.with_suffix(".pid")


@bridge_app.command("start")
def bridge_start(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
    daemon: bool = typer.Option(
        False,
        "--daemon",
        "-d",
        help="Run in background",
    ),
) -> None:
    """Start the Telegram bridge."""
    import os
    import subprocess
    import sys

    try:
        config = load_config(config_path)
    except ConfigError as e:
        console.print(f"[red]Error loading config:[/red] {e}")
        raise typer.Exit(1)

    if config.transport.type.value != "telegram":
        console.print("[yellow]Transport is not set to 'telegram' in config.[/yellow]")
        console.print("Set transport.type: telegram to use the bridge.")
        raise typer.Exit(1)

    telegram_config = config.transport.telegram
    if not telegram_config:
        console.print("[red]Telegram config not found.[/red]")
        raise typer.Exit(1)

    socket_path = telegram_config.socket_path.expanduser().resolve()
    pid_file = _get_bridge_pid_file(socket_path)

    # Check if already running
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
            console.print(f"[yellow]Bridge already running (PID {pid})[/yellow]")
            raise typer.Exit(1)
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)

    # Get bot token from environment
    bot_token = os.environ.get(telegram_config.bot_token_env)
    if not bot_token:
        console.print(
            f"[red]Bot token not found.[/red] Set {telegram_config.bot_token_env} environment variable."
        )
        raise typer.Exit(1)

    if daemon:
        # Start as daemon
        cmd = [
            sys.executable,
            "-m",
            "dev_sync.bridge",
            "--socket-path",
            str(socket_path),
            "--bot-token",
            bot_token,
            "--chat-id",
            str(telegram_config.chat_id),
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        pid_file.write_text(str(proc.pid))
        console.print(f"[green]Bridge started (PID {proc.pid})[/green]")
    else:
        # Run in foreground
        import asyncio

        from dev_sync.bridge import BridgeServer

        console.print(f"Starting bridge on {socket_path}")
        console.print("Press Ctrl+C to stop")

        server = BridgeServer(
            socket_path=socket_path,
            bot_token=bot_token,
            chat_id=telegram_config.chat_id,
        )

        try:
            asyncio.run(server.start())
        except KeyboardInterrupt:
            console.print("\n[yellow]Shutting down...[/yellow]")


@bridge_app.command("stop")
def bridge_stop(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Stop the Telegram bridge."""
    import os
    import signal

    socket_path = _get_socket_path(config_path)
    pid_file = _get_bridge_pid_file(socket_path)

    if not pid_file.exists():
        console.print("[yellow]Bridge not running (no PID file)[/yellow]")
        return

    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        console.print(f"[green]Stopped bridge (PID {pid})[/green]")
        pid_file.unlink(missing_ok=True)
    except ProcessLookupError:
        console.print("[yellow]Bridge process not found[/yellow]")
        pid_file.unlink(missing_ok=True)
    except ValueError:
        console.print("[red]Invalid PID file[/red]")
        pid_file.unlink(missing_ok=True)


@bridge_app.command("status")
def bridge_status(
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Check bridge status."""
    import os

    socket_path = _get_socket_path(config_path)
    pid_file = _get_bridge_pid_file(socket_path)

    # Check PID file
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, 0)
            console.print(f"[green]Bridge running (PID {pid})[/green]")
            console.print(f"Socket: {socket_path}")
            return
        except (ProcessLookupError, ValueError):
            pass

    # Check socket file
    if socket_path.exists():
        console.print("[yellow]Socket exists but no running process[/yellow]")
        console.print(f"Socket: {socket_path}")
    else:
        console.print("[dim]Bridge not running[/dim]")


@bridge_app.command("test")
def bridge_test(
    message: str = typer.Option(
        "Test message from dev-sync bridge",
        "--message",
        "-m",
        help="Message to send",
    ),
    config_path: str = typer.Option(
        "config/orchestrator.yaml",
        "--config",
        "-c",
        help="Path to orchestrator.yaml",
    ),
) -> None:
    """Send a test message to verify bridge is working."""
    import asyncio

    socket_path = _get_socket_path(config_path)

    if not socket_path.exists():
        console.print("[red]Bridge not running.[/red] Start it with: dev-sync bridge start")
        raise typer.Exit(1)

    async def send_test():
        from dev_sync.transports import SocketTransport

        transport = SocketTransport(socket_path)
        try:
            await transport.connect()
            await transport.send(message)
            console.print("[green]Message sent successfully![/green]")
        finally:
            await transport.close()

    try:
        asyncio.run(send_test())
    except Exception as e:
        console.print(f"[red]Failed to send message:[/red] {e}")
        raise typer.Exit(1)
```

- [ ] **Step 2: Test CLI help**

Run:
```bash
dev-sync bridge --help
```
Expected: Shows start, stop, status, test subcommands

- [ ] **Step 3: Test bridge status (not running)**

Run:
```bash
dev-sync bridge status
```
Expected: Shows "Bridge not running"

- [ ] **Step 4: Commit**

```bash
git add src/dev_sync/cli.py
git commit -m "feat: add bridge CLI commands (start, stop, status, test)"
```

---

### Task 9: Add bridge __main__.py for daemon mode

**Files:**
- Create: `src/dev_sync/bridge/__main__.py`

- [ ] **Step 1: Create bridge __main__.py**

Create `src/dev_sync/bridge/__main__.py`:
```python
"""Bridge process entry point for daemon mode."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
from pathlib import Path

from dev_sync.bridge.server import BridgeServer


def main() -> None:
    parser = argparse.ArgumentParser(description="dev-sync Telegram bridge")
    parser.add_argument("--socket-path", required=True, help="Unix socket path")
    parser.add_argument("--bot-token", required=True, help="Telegram bot token")
    parser.add_argument("--chat-id", type=int, required=True, help="Telegram chat ID")
    args = parser.parse_args()

    socket_path = Path(args.socket_path)
    server = BridgeServer(
        socket_path=socket_path,
        bot_token=args.bot_token,
        chat_id=args.chat_id,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def handle_signal(sig: int) -> None:
        loop.create_task(server.stop())

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal, sig)

    try:
        loop.run_until_complete(server.start())
    except asyncio.CancelledError:
        pass
    finally:
        loop.close()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Test module can be invoked**

Run:
```bash
python -m dev_sync.bridge --help
```
Expected: Shows help with socket-path, bot-token, chat-id options

- [ ] **Step 3: Commit**

```bash
git add src/dev_sync/bridge/__main__.py
git commit -m "feat: add bridge __main__.py for daemon mode"
```

---

### Task 10: Add get_transport factory function

**Files:**
- Modify: `src/dev_sync/transports/__init__.py`
- Modify: `tests/test_transport.py`

- [ ] **Step 1: Write failing test for get_transport**

Add to `tests/test_transport.py`:
```python
class TestGetTransport:
    def test_get_file_mock_transport(self, tmp_path) -> None:
        """Should return FileMockTransport for file_mock type."""
        from dev_sync.core.config import FileMockConfig, TransportConfig, TransportType
        from dev_sync.transports import get_transport

        config = TransportConfig(
            type=TransportType.FILE_MOCK,
            file_mock=FileMockConfig(
                inbox=tmp_path / "inbox.txt",
                outbox=tmp_path / "outbox.txt",
            ),
        )
        (tmp_path / "inbox.txt").touch()
        (tmp_path / "outbox.txt").touch()

        transport = get_transport(config)
        assert transport.__class__.__name__ == "FileMockTransport"

    def test_get_socket_transport(self, tmp_path) -> None:
        """Should return SocketTransport for telegram type."""
        from dev_sync.core.config import TelegramConfig, TransportConfig, TransportType
        from dev_sync.transports import get_transport

        config = TransportConfig(
            type=TransportType.TELEGRAM,
            telegram=TelegramConfig(
                chat_id=123,
                socket_path=tmp_path / "test.sock",
            ),
        )

        transport = get_transport(config)
        assert transport.__class__.__name__ == "SocketTransport"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:
```bash
pytest tests/test_transport.py::TestGetTransport -v
```
Expected: FAIL with "cannot import name 'get_transport'"

- [ ] **Step 3: Implement get_transport**

Update `src/dev_sync/transports/__init__.py`:
```python
"""Transport abstraction for orchestrator communication."""

from dev_sync.core.config import TransportConfig, TransportType
from dev_sync.transports.base import Transport, TransportError
from dev_sync.transports.file_mock import FileMockTransport
from dev_sync.transports.socket_client import SocketTransport


def get_transport(config: TransportConfig) -> Transport:
    """Create transport instance from config."""
    if config.type == TransportType.FILE_MOCK:
        assert config.file_mock is not None
        return FileMockTransport(
            inbox=config.file_mock.inbox,
            outbox=config.file_mock.outbox,
        )

    if config.type == TransportType.TELEGRAM:
        assert config.telegram is not None
        return SocketTransport(
            socket_path=config.telegram.socket_path,
        )

    raise TransportError(f"Unknown transport type: {config.type}")


__all__ = [
    "FileMockTransport",
    "SocketTransport",
    "Transport",
    "TransportError",
    "get_transport",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run:
```bash
pytest tests/test_transport.py -v
```
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add src/dev_sync/transports/ tests/test_transport.py
git commit -m "feat: add get_transport factory function"
```

---

### Task 11: Run full test suite and verify phase gate

**Files:** None (verification only)

- [ ] **Step 1: Run all tests**

Run:
```bash
pytest tests/ -v
```
Expected: All tests pass

- [ ] **Step 2: Run linter**

Run:
```bash
ruff check src/ tests/
```
Expected: No errors

- [ ] **Step 3: Verify CLI commands**

Run:
```bash
dev-sync bridge --help
dev-sync bridge status
```
Expected: Help shows commands, status shows "not running"

- [ ] **Step 4: Test bridge end-to-end (manual)**

To verify the phase gate (`dev-sync bridge test` delivers message to phone):

1. Set up Telegram bot:
   - Create bot via @BotFather, get token
   - Get chat ID by messaging bot and checking `/getUpdates`

2. Configure:
   ```yaml
   # config/orchestrator.yaml
   transport:
     type: telegram
     telegram:
       chat_id: YOUR_CHAT_ID
   ```

3. Set environment:
   ```bash
   export DEV_SYNC_TELEGRAM_TOKEN="your-bot-token"
   ```

4. Start bridge:
   ```bash
   dev-sync bridge start
   ```

5. Test message:
   ```bash
   dev-sync bridge test -m "Hello from dev-sync!"
   ```

Expected: Message appears in Telegram chat

- [ ] **Step 5: Commit final state**

```bash
git add -A
git status
# If any uncommitted changes:
git commit -m "chore: phase 2 complete - telegram bridge"
```

---

## Phase Gate Verification

**Phase 2 is complete when:**

1. `dev-sync bridge start` starts the bridge process
2. `dev-sync bridge status` shows bridge status
3. `dev-sync bridge test` delivers message to phone
4. `dev-sync bridge stop` stops the bridge process
