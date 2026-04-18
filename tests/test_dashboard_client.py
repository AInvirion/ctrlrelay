"""Tests for dashboard client."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest


class TestDashboardClient:
    @pytest.mark.asyncio
    async def test_push_event_sends_to_server(self) -> None:
        """Should POST event to dashboard server."""
        from ctrlrelay.dashboard.client import DashboardClient, EventPayload

        client = DashboardClient(
            url="https://dashboard.example.com",
            auth_token="test-token",
            node_id="test-node",
        )

        event = EventPayload(
            level="info",
            pipeline="secops",
            repo="owner/repo",
            message="Merged 3 PRs",
        )

        mock_response = AsyncMock()
        mock_response.status_code = 200

        with patch("httpx.AsyncClient.post", return_value=mock_response) as mock_post:
            await client.push_event(event)

            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert "/event" in call_args[0][0]

    @pytest.mark.asyncio
    async def test_push_event_queues_on_failure(self, tmp_path: Path) -> None:
        """Should queue event when server unreachable."""
        from ctrlrelay.dashboard.client import DashboardClient, EventPayload

        client = DashboardClient(
            url="https://dashboard.example.com",
            auth_token="test-token",
            node_id="test-node",
            queue_dir=tmp_path,
        )

        event = EventPayload(
            level="info",
            pipeline="secops",
            repo="owner/repo",
            message="Test event",
        )

        with patch("httpx.AsyncClient.post", side_effect=httpx.TimeoutException("timeout")):
            result = await client.push_event(event)

            assert result is False
            assert client.queue_size == 1

            queue_file = tmp_path / "event_queue.json"
            assert queue_file.exists()

    @pytest.mark.asyncio
    async def test_drain_queue_sends_queued_events(self, tmp_path: Path) -> None:
        """Should send queued events when connection restored."""
        from ctrlrelay.dashboard.client import DashboardClient

        queue_file = tmp_path / "event_queue.json"
        queue_file.write_text(json.dumps([
            {
                "level": "info", "pipeline": "secops", "repo": "r1",
                "message": "m1", "timestamp": "t1", "details": {}
            },
            {
                "level": "info", "pipeline": "secops", "repo": "r2",
                "message": "m2", "timestamp": "t2", "details": {}
            },
        ]))

        client = DashboardClient(
            url="https://dashboard.example.com",
            auth_token="test-token",
            node_id="test-node",
            queue_dir=tmp_path,
        )

        assert client.queue_size == 2

        mock_response = AsyncMock()
        mock_response.status_code = 200
        mock_response.raise_for_status = lambda: None

        with patch("httpx.AsyncClient.post", return_value=mock_response):
            sent = await client.drain_queue()

            assert sent == 2
            assert client.queue_size == 0
