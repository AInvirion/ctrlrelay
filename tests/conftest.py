"""Pytest fixtures for ctrlrelay tests."""

from pathlib import Path

import pytest
import yaml

# An autouse fixture used to patch ctrlrelay.bridge.server.TelegramHandler for
# every test in test_bridge_server, because the server constructed its own
# handler and there was no other way to keep the suite off the network. The
# server takes a ChatHandler now, so tests inject FakeChatHandler below and
# the patch is gone — nothing is monkeypatched into the module under test
# just to make it testable.


@pytest.fixture
def sample_config_dict() -> dict:
    """Minimal valid configuration dictionary."""
    return {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": "~/.ctrlrelay/state.db",
            "worktrees": "~/.ctrlrelay/worktrees",
            "bare_repos": "~/.ctrlrelay/repos",
            "contexts": "~/.ctrlrelay/contexts",
            "skills": "~/.ctrlrelay/skills",
        },
        "claude": {
            "binary": "claude",
            "default_timeout_seconds": 1800,
            "output_format": "json",
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": "~/.ctrlrelay/inbox.txt",
                "outbox": "~/.ctrlrelay/outbox.txt",
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


class FakeChatHandler:
    """A ChatHandler for tests, with no network and no library behind it.

    The suite used to patch `BridgeServer._telegram` after construction,
    which only worked because the server built its own handler. It takes
    one now, so the fake is injected instead — and the tests stop
    depending on which chat app the bridge happens to use.

    `post_ids` is a canned list so a test can predict the ids it will be
    given; `ask`/`send` are plain methods so a test can still replace them
    with an AsyncMock when it wants to assert on calls or raise.
    """

    def __init__(
        self,
        *,
        name: str = "fakechat",
        post_ids: list[str] | None = None,
        ambiguous: tuple[type[BaseException], ...] = (),
    ) -> None:
        self._name = name
        self._post_ids = list(post_ids or [])
        self._next = 0
        self._ambiguous = ambiguous
        self.sent: list[str] = []
        self.asked: list[tuple[str, list[str] | None]] = []
        self.closed = False
        self.polling_handler = None

    def _mint(self) -> str:
        if self._next < len(self._post_ids):
            pid = self._post_ids[self._next]
        else:
            pid = f"post-{self._next}"
        self._next += 1
        return pid

    @property
    def transport_name(self) -> str:
        return self._name

    @property
    def destination(self) -> str:
        return f"{self._name}:channel=test"

    async def send(self, text: str) -> str:
        self.sent.append(text)
        return self._mint()

    async def ask(self, question: str, options: list[str] | None = None) -> str:
        self.asked.append((question, options))
        return self._mint()

    async def start_polling(self, handler) -> None:
        self.polling_handler = handler

    async def stop_polling(self) -> None:
        self.polling_handler = None

    async def close(self) -> None:
        self.closed = True
        await self.stop_polling()

    def is_ambiguous_delivery(self, exc: BaseException) -> bool:
        return isinstance(exc, self._ambiguous)


@pytest.fixture
def fake_handler() -> FakeChatHandler:
    return FakeChatHandler()
