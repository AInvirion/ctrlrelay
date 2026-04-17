"""Tests for bridge server."""

import asyncio
import os
import shutil
import stat
import tempfile
from pathlib import Path

import pytest


class TestBridgeServer:
    @pytest.fixture
    def socket_path(self):
        # tmp_path can exceed AF_UNIX's 104-char limit on macOS; use a short dir.
        d = tempfile.mkdtemp()
        yield Path(d) / "b.sock"
        shutil.rmtree(d, ignore_errors=True)

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
        from dev_sync.bridge.protocol import (
            BridgeMessage,
            BridgeOp,
            parse_message,
            serialize_message,
        )
        from dev_sync.bridge.server import BridgeServer

        server = BridgeServer(socket_path=socket_path, bot_token="test", chat_id=123)
        task = asyncio.create_task(server.start())
        await asyncio.sleep(0.1)

        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        ping = serialize_message(BridgeMessage(op=BridgeOp.PING))
        writer.write(ping.encode())
        await writer.drain()

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
