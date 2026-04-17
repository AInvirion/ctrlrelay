"""Pytest fixtures for dev-sync tests."""

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml


@pytest.fixture(autouse=True)
def mock_telegram_handler(request):
    """Auto-mock TelegramHandler for bridge server tests to avoid real API calls."""
    if "test_bridge_server" in request.fspath.basename:
        mock_handler = AsyncMock()
        with patch("dev_sync.bridge.server.TelegramHandler", return_value=mock_handler):
            yield mock_handler
    else:
        yield


@pytest.fixture
def sample_config_dict() -> dict:
    """Minimal valid configuration dictionary."""
    return {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": "~/.dev-sync/state.db",
            "worktrees": "~/.dev-sync/worktrees",
            "bare_repos": "~/.dev-sync/repos",
            "contexts": "~/dev-sync/contexts",
            "skills": "~/dev-sync/skills",
        },
        "claude": {
            "binary": "claude",
            "default_timeout_seconds": 1800,
            "output_format": "json",
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": "~/.dev-sync/inbox.txt",
                "outbox": "~/.dev-sync/outbox.txt",
            },
        },
        "dashboard": {
            "enabled": False,
        },
        "repos": [],
    }


@pytest.fixture
def sample_config_file(sample_config_dict: dict, tmp_path: Path) -> Path:
    """Write sample config to a temporary file."""
    config_path = tmp_path / "orchestrator.yaml"
    config_path.write_text(yaml.dump(sample_config_dict))
    return config_path
