"""Which chat transport the orchestrator builds, and from where (#181).

The defect: the scheduled secops sweep and the pending-resume sweeper
each resolved the bridge socket with

    if config.transport.type.value == "telegram" and config.transport.telegram:

so with Mattermost configured both conditions were false, both built no
transport, and neither said so. Every BLOCKED session still persisted a
`pending_resumes` row, so the run looked healthy from the database while
the operator was never asked anything. Measured on 2026-10-06: 16
questions written and 0 delivered.
"""

from __future__ import annotations

import ast
from pathlib import Path

import yaml


def _config(tmp_path: Path, transport: dict) -> object:
    from ctrlrelay.core.config import load_config

    data = {
        "version": "1",
        "node_id": "test-node",
        "timezone": "UTC",
        "paths": {
            "state_db": str(tmp_path / "state.db"),
            "worktrees": str(tmp_path / "worktrees"),
            "bare_repos": str(tmp_path / "repos"),
            "contexts": str(tmp_path / "contexts"),
            "skills": str(tmp_path / "skills"),
        },
        "agent": {"binary": "agent-bin", "default_timeout_seconds": 1800},
        "transport": transport,
        "dashboard": {"enabled": False},
        "repos": [],
    }
    p = tmp_path / "orchestrator.yaml"
    p.write_text(yaml.dump(data))
    return load_config(p)


class TestBridgeSocketSettings:
    """The helper had no test at all before this change."""

    def test_mattermost_resolves_its_own_socket_and_timeout(
        self, tmp_path: Path
    ) -> None:
        """The case #181 was about: not telegram, still a chat transport."""
        from ctrlrelay.cli import _bridge_socket_settings

        mm_sock = tmp_path / "mm.sock"
        config = _config(
            tmp_path,
            {
                "type": "mattermost",
                # Both blocks present and DIFFERENT, so a helper that
                # reached for the wrong one would resolve a real path and
                # a real timeout rather than failing visibly.
                "telegram": {
                    "socket_path": str(tmp_path / "tg.sock"),
                    "bot_token_env": "TG",
                    "chat_id": 1,
                    "ask_timeout_seconds": 111,
                },
                "mattermost": {
                    "url": "https://chat.example.test",
                    "socket_path": str(mm_sock),
                    "bot_token_env": "MM",
                    "channel_id": "c" * 26,
                    "ask_timeout_seconds": 222,
                },
            },
        )

        settings = _bridge_socket_settings(config)

        assert settings is not None, (
            "Mattermost is a configured chat transport; None here is the "
            "#181 defect and means every blocked question goes undelivered"
        )
        sock, timeout = settings
        assert sock == mm_sock.expanduser().resolve()
        assert timeout == 222

    def test_telegram_still_resolves_its_own(self, tmp_path: Path) -> None:
        """The paired case, so the test fails both ways round.

        Without it, a helper hardcoded to Mattermost would satisfy the
        test above — the same defect pointed the other way.
        """
        from ctrlrelay.cli import _bridge_socket_settings

        tg_sock = tmp_path / "tg.sock"
        config = _config(
            tmp_path,
            {
                "type": "telegram",
                "telegram": {
                    "socket_path": str(tg_sock),
                    "bot_token_env": "TG",
                    "chat_id": 1,
                    "ask_timeout_seconds": 111,
                },
                "mattermost": {
                    "url": "https://chat.example.test",
                    "socket_path": str(tmp_path / "mm.sock"),
                    "bot_token_env": "MM",
                    "channel_id": "c" * 26,
                    "ask_timeout_seconds": 222,
                },
            },
        )

        settings = _bridge_socket_settings(config)

        assert settings is not None
        sock, timeout = settings
        assert sock == tg_sock.expanduser().resolve()
        assert timeout == 111

    def test_a_non_chat_transport_is_none_not_a_default(
        self, tmp_path: Path
    ) -> None:
        """`None` must mean "there is no channel", distinctly.

        Callers treat it differently from a missing socket, so returning
        a plausible default here would silently opt them back into the
        bug.

        `file_mock` is the reachable way to get it. A chat type with its
        block omitted is NOT: the config validator refuses that outright
        (`mattermost config required when type is 'mattermost'`), so
        testing it would have asserted on an input nobody can build.
        """
        from ctrlrelay.cli import _bridge_socket_settings

        config = _config(
            tmp_path,
            {
                "type": "file_mock",
                "file_mock": {
                    "inbox": str(tmp_path / "in"),
                    "outbox": str(tmp_path / "out"),
                },
            },
        )
        assert _bridge_socket_settings(config) is None

        # Paired in the same test so it cannot pass for a helper that
        # returns None unconditionally - which `is None` alone would.
        chat = _config(
            tmp_path,
            {
                "type": "mattermost",
                "mattermost": {
                    "url": "https://chat.example.test",
                    "socket_path": str(tmp_path / "mm.sock"),
                    "bot_token_env": "MM",
                    "channel_id": "c" * 26,
                },
            },
        )
        assert _bridge_socket_settings(chat) is not None


class TestOnlyTheHelperNamesATransport:
    """Holds the shape, so an eighth call site cannot reintroduce it.

    The transport names are read from `TransportConfig`'s own fields
    rather than listed, so a transport added later is covered the day it
    is added rather than the day somebody remembers this test.

    This is the guard that would have caught #181. `_bridge_socket_settings`
    was added precisely to end this fault and its own docstring names the
    three sites it converted — two further sites kept the old gate anyway,
    and nothing failed.
    """

    def test_cli_names_a_specific_transport_only_inside_the_helper(
        self,
    ) -> None:
        import ctrlrelay.cli as cli_mod

        source = Path(cli_mod.__file__).read_text()
        tree = ast.parse(source)

        # Derived from the model, never listed here. A hardcoded
        # ("telegram", "mattermost") is an enumeration wearing a guard's
        # clothes: adding a `slack` block would leave this green while
        # a new call site reached for it directly, which is #181 again
        # with a different name.
        from ctrlrelay.core.config import TransportConfig

        transport_blocks = {
            name for name in TransportConfig.model_fields if name != "type"
        }
        assert {"telegram", "mattermost"} <= transport_blocks, (
            f"model fields moved; guard is reading {transport_blocks}"
        )

        allowed = ("_bridge_socket_settings", "_active_chat_config")
        offenders: list[str] = []
        seen = 0

        # Map every line of the allowed function so membership is a line
        # lookup rather than a name match on the enclosing scope.
        allowed_lines: set[int] = set()
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in allowed:
                found.add(node.name)
                allowed_lines |= set(
                    range(node.lineno, (node.end_lineno or node.lineno) + 1)
                )
        assert found == set(allowed), f"missing from cli.py: {set(allowed) - found}"

        for node in ast.walk(tree):
            # Deliberately NOT restricted to `config.transport.<name>`.
            # Requiring that base let two bypasses through: an aliased
            # local (`t = config.transport; t.telegram`) and a
            # `getattr(..., "telegram")`. Broadening it immediately found
            # a third live instance of #181 in the question-expiry
            # sweeper that neither review round had seen, so the narrower
            # rule was not merely weaker - it was already failing.
            name = None
            if isinstance(node, ast.Attribute) and node.attr in transport_blocks:
                name = node.attr
            elif (
                isinstance(node, ast.Call)
                and getattr(node.func, "id", None) == "getattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in transport_blocks
            ):
                name = node.args[1].value
            if name is None:
                continue
            seen += 1
            if node.lineno not in allowed_lines:
                offenders.append(f"cli.py:{node.lineno} -> {name}")

        # Negative control: if the walk matched nothing, an empty
        # offender list would be reported for the same reason a passing
        # one is. The helper itself must contain at least two.
        assert seen >= 2, f"walk found only {seen} transport references"

        assert offenders == [], (
            f"only {' and '.join(allowed)} may name a specific "
            "transport's config block; these re-derive it and will skip "
            f"whichever transport they do not name (#181): {offenders}"
        )


class TestPerTransportSettingsFollowTheActiveTransport:
    """#181's third instance, found by broadening the guard above.

    The question-expiry sweeper read `question_ttl_seconds` off
    `config.transport.telegram` whatever transport was running. On this
    deployment it returned a real number only because a vestigial
    telegram block was still sitting in the config file; deleting that
    block would have made `ttl` None, taken the `if not ttl: return`
    path, and stopped questions expiring with nothing logged.
    """

    def test_the_ttl_comes_from_the_running_transport(
        self, tmp_path: Path
    ) -> None:
        from ctrlrelay.cli import _active_chat_config

        config = _config(
            tmp_path,
            {
                "type": "mattermost",
                "telegram": {
                    "socket_path": str(tmp_path / "tg.sock"),
                    "bot_token_env": "TG",
                    "chat_id": 1,
                    "question_ttl_seconds": 3600,
                },
                "mattermost": {
                    "url": "https://chat.example.test",
                    "socket_path": str(tmp_path / "mm.sock"),
                    "bot_token_env": "MM",
                    "channel_id": "c" * 26,
                    "question_ttl_seconds": 7200,
                },
            },
        )

        chat = _active_chat_config(config)
        assert chat is not None
        assert chat.question_ttl_seconds == 7200, (
            "the TTL must come from the transport that is running; 3600 "
            "here means it was read from the telegram block"
        )

    def test_a_deployment_with_no_telegram_block_still_has_a_ttl(
        self, tmp_path: Path
    ) -> None:
        """The case that would have gone silent.

        A Mattermost deployment with the vestigial telegram block
        removed - which is the tidy-up anyone would eventually do.
        """
        from ctrlrelay.cli import _active_chat_config

        config = _config(
            tmp_path,
            {
                "type": "mattermost",
                "mattermost": {
                    "url": "https://chat.example.test",
                    "socket_path": str(tmp_path / "mm.sock"),
                    "bot_token_env": "MM",
                    "channel_id": "c" * 26,
                },
            },
        )

        chat = _active_chat_config(config)
        assert chat is not None
        assert chat.question_ttl_seconds > 0
