"""Adding a machine: the guards and the check command (#250)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from ctrlrelay.core.claims import HELD, WORKING, ClaimSettings, claim_body
from ctrlrelay.core.node_check import (
    claim_refreshed_by_another,
    peers_with_my_name,
    run_checks,
)

NOW = 1_793_490_000.0
ME = ClaimSettings(node="node-a", account="bot", refresh_seconds=300)


def _comment(body: str, updated: float) -> dict:
    iso = datetime.fromtimestamp(updated, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"id": 1, "body": body, "user": {"login": "bot"},
            "created_at": iso, "updated_at": iso}


class TestNodeId:
    def _config(self, node_id):
        from ctrlrelay.core.config import Config

        return Config.model_validate({
            "node_id": node_id,
            "paths": {k: "/tmp/x" for k in
                      ("state_db", "worktrees", "bare_repos", "contexts", "skills")},
            "transport": {"type": "file_mock",
                          "file_mock": {"inbox": "/tmp/i", "outbox": "/tmp/o"}},
        })

    @pytest.mark.parametrize("bad", ["my-laptop", "localhost", "Unknown", " studio-mac "])
    def test_a_placeholder_is_refused_with_what_to_do(self, bad) -> None:
        from pydantic import ValidationError

        with pytest.raises(
            ValidationError, match="does not name this machine: set node_id in",
        ):
            self._config(bad)

    def test_a_real_name_and_the_hostname_default_are_fine(self, monkeypatch) -> None:
        import socket

        assert self._config("node-b").node_id == "node-b"
        monkeypatch.setattr(socket, "gethostname", lambda: "box-7")
        assert self._config(None).node_id == "box-7"

    def test_a_hostname_of_localhost_is_refused_too(self, monkeypatch) -> None:
        import socket

        from pydantic import ValidationError

        monkeypatch.setattr(socket, "gethostname", lambda: "localhost")
        with pytest.raises(ValidationError, match="does not name this machine"):
            self._config(None)


class TestDuplicateFromClaims:
    def test_a_claim_under_my_name_written_after_i_started_is_another_process(self) -> None:
        started = NOW - 100
        comments = [_comment(
            claim_body("node-a", "x1", WORKING, NOW - 60, run="other"), NOW - 60,
        )]
        assert claim_refreshed_by_another(comments, ME, started_at=started)

    def test_my_own_leftover_is_not_however_recent(self) -> None:
        # Written 5 seconds before this process started: a restart within
        # the refresh interval leaves exactly this.
        started = NOW - 55
        comments = [_comment(
            claim_body("node-a", "x1", WORKING, NOW - 60, run="earlier"), NOW - 60,
        )]
        assert not claim_refreshed_by_another(comments, ME, started_at=started)

    def test_held_claims_and_other_machines_are_not(self) -> None:
        comments = [
            _comment(claim_body("node-a", "x1", HELD, NOW - 10, run="other"), NOW - 10),
            _comment(claim_body("node-b", "x2", WORKING, NOW - 10, run="other"), NOW - 10),
        ]
        assert not claim_refreshed_by_another(comments, ME, started_at=NOW - 100)

    def test_a_claim_this_process_wrote_never_counts(self) -> None:
        # Written after start, by this process: the issue came round again
        # after this process ran it.
        comments = [_comment(claim_body("node-a", "x1", WORKING, NOW - 10), NOW - 10)]
        assert not claim_refreshed_by_another(comments, ME, started_at=NOW - 100)

    def test_a_leftover_is_judged_by_its_own_clock_not_githubs(self) -> None:
        # Written before this restart by this machine's clock; GitHub's
        # clock, ahead of this one, stamped the comment after it.
        comments = [_comment(
            claim_body("node-a", "x1", WORKING, NOW - 60, run="earlier"), NOW + 30,
        )]
        assert not claim_refreshed_by_another(comments, ME, started_at=NOW - 55)

    def test_a_claim_from_before_the_run_marker_is_not_counted(self) -> None:
        body = claim_body("node-a", "x1", WORKING, NOW - 10, run="gone").replace(
            " run=gone", "",
        )
        assert "run=" not in body
        assert not claim_refreshed_by_another(
            [_comment(body, NOW - 10)], ME, started_at=NOW - 100,
        )


class TestPeersWithMyName:
    @pytest.mark.asyncio
    async def test_finds_the_peer_answering_as_me_and_ignores_the_rest(self) -> None:
        answers = {
            "http://b": {"host": "b", "generated_ts": 1, "node": {"name": "node-b"}},
            "http://dup": {"host": "x", "generated_ts": 1, "node": {"name": "node-a"}},
        }

        def fetch(url, *, timeout):
            if url not in answers:
                raise ConnectionRefusedError
            return answers[url]

        found = await peers_with_my_name(
            "node-a", ["http://b", "http://dup", "http://down"], fetch=fetch, timeout=1,
        )
        assert found == ["http://dup"]


class TestRunChecks:
    def _config(self, peers):
        return SimpleNamespace(
            node_id="node-a",
            fleet=SimpleNamespace(peers=peers, timeout_seconds=1),
        )

    @pytest.mark.asyncio
    async def test_every_line_and_its_verdict(self) -> None:
        async def login() -> str:
            return "the-account"

        async def probe():
            return SimpleNamespace(windows={"five_hour": SimpleNamespace(used=0.12)})

        def fetch(url, *, timeout):
            if url == "http://down":
                raise ConnectionRefusedError
            return {"host": "x", "generated_ts": 1,
                    "node": {"name": "node-b" if url == "http://b" else "node-a"}}

        checks = await run_checks(
            self._config(["http://b", "http://down", "http://self"]),
            github_login=login, agent_probe=probe, fetch=fetch,
        )
        got = {c.name: (c.ok, c.detail) for c in checks}
        assert got["node_id"][0] is True
        assert got["GitHub login"] == (True, "the-account")
        assert got["agent login"][0] is True and "12%" in got["agent login"][1]
        assert got["peer http://b"][0] is True
        assert got["peer http://down"][0] is False
        assert got["peer http://down"][1].startswith("refused. Check the URL")
        assert got["peer http://self"][0] is False
        assert "own name" in got["peer http://self"][1]

    @pytest.mark.asyncio
    async def test_failures_are_lines_not_crashes(self) -> None:
        async def login() -> str:
            raise RuntimeError("gh: not logged in")

        async def probe():
            return None

        checks = await run_checks(
            self._config([]), github_login=login, agent_probe=probe,
            fetch=lambda *a, **k: {},
        )
        got = {c.name: c for c in checks}
        assert got["GitHub login"].ok is False and "not logged in" in got["GitHub login"].detail
        assert "gh auth login" in got["GitHub login"].detail
        assert got["agent login"].ok is False and "log in" in got["agent login"].detail
        assert got["peers"].ok is None

    @pytest.mark.asyncio
    async def test_no_agent_skips_the_call(self) -> None:
        async def login() -> str:
            return "a"

        checks = await run_checks(
            self._config([]), github_login=login, agent_probe=None,
            fetch=lambda *a, **k: {},
        )
        assert [c.ok for c in checks if c.name == "agent login"] == [None]


class TestTheCommand:
    def test_exit_code_is_1_when_a_check_fails(self, tmp_path, monkeypatch) -> None:
        from typer.testing import CliRunner

        import ctrlrelay.core.node_check as nc
        from ctrlrelay.cli import app

        cfg = tmp_path / "orchestrator.yaml"
        cfg.write_text(
            'version: "1"\nnode_id: "node-a"\n'
            f'paths: {{state_db: "{tmp_path}/s.db", worktrees: "{tmp_path}/w", '
            f'bare_repos: "{tmp_path}/r", contexts: "{tmp_path}/c", skills: "{tmp_path}/k"}}\n'
            f'transport: {{type: file_mock, file_mock: {{inbox: "{tmp_path}/i", '
            f'outbox: "{tmp_path}/o"}}}}\n'
        )

        async def fake_run_checks(config, **kw):
            return [nc.Check("node_id", True, "node-a"), nc.Check("GitHub login", False, "no")]

        monkeypatch.setattr(nc, "run_checks", fake_run_checks)
        result = CliRunner().invoke(app, ["node", "check", "--no-agent", "-c", str(cfg)])
        assert result.exit_code == 1
        assert "GitHub login: no" in result.output


class TestThePeekWarns:
    @pytest.mark.asyncio
    async def test_the_claim_peek_reports_a_duplicate_name(self, monkeypatch) -> None:
        import time
        from unittest.mock import AsyncMock

        import ctrlrelay.cli as cli_mod
        from ctrlrelay.cli import peek_claim_owner

        now = time.time()
        monkeypatch.setattr(cli_mod, "PROCESS_STARTED_AT", now - 60)
        github = AsyncMock()
        github.list_issue_comments.return_value = [
            _comment(claim_body("node-a", "x1", WORKING, now - 30, run="other"), now - 30),
        ]
        seen = []
        await peek_claim_owner(github, "o/r", 7, ME, on_duplicate=lambda r, n: seen.append((r, n)))
        assert seen == [("o/r", 7)]



class TestRoundOneFixes:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("answer", [
        None, [], {}, {"node": {"name": 5}, "generated_ts": 1}, {"host": "unrelated"},
    ])
    async def test_an_answer_that_is_not_a_status_page_fails(self, answer) -> None:
        async def login() -> str:
            return "a"

        checks = await run_checks(
            SimpleNamespace(node_id="node-a", fleet=SimpleNamespace(
                peers=["http://x"], timeout_seconds=1)),
            github_login=login, agent_probe=None, fetch=lambda url, *, timeout: answer,
        )
        [peer] = [c for c in checks if c.name == "peer http://x"]
        assert peer.ok is False and "not with a ctrlrelay status page" in peer.detail

    def test_a_process_exits_while_a_peer_read_is_still_hung(self, tmp_path) -> None:
        import subprocess
        import sys
        import time as _time

        script = tmp_path / "hang.py"
        script.write_text(
            "import asyncio, threading\n"
            "from ctrlrelay.core.node_check import fetch_detached\n"
            "def stuck(url, *, timeout):\n"
            "    threading.Event().wait()\n"
            "async def main():\n"
            "    try:\n"
            "        await fetch_detached(stuck, 'http://x', timeout=0.05)\n"
            "    except TimeoutError:\n"
            "        print('gave up')\n"
            "asyncio.run(main())\n"
        )
        started = _time.monotonic()
        out = subprocess.run([sys.executable, str(script)], capture_output=True,
                             text=True, timeout=20)
        assert out.returncode == 0, out.stderr
        assert "gave up" in out.stdout
        assert _time.monotonic() - started < 15

    @pytest.mark.asyncio
    async def test_a_hung_peer_is_given_up_without_holding_the_process(self) -> None:
        import threading

        from ctrlrelay.core.node_check import fetch_detached

        hold = threading.Event()

        def stuck(url, *, timeout):
            hold.wait(30)
            return {}

        with pytest.raises(TimeoutError):
            await fetch_detached(stuck, "http://x", timeout=0.05)
        stuck_threads = [t for t in threading.enumerate() if t.name == "ctrlrelay-peer-check"]
        assert stuck_threads and all(t.daemon for t in stuck_threads)
        hold.set()

    def test_no_agent_never_looks_for_the_agent(self, tmp_path, monkeypatch) -> None:
        from typer.testing import CliRunner

        import ctrlrelay.core.dispatcher as disp
        import ctrlrelay.core.node_check as nc
        from ctrlrelay.cli import app

        def boom():
            raise RuntimeError("no agent binary here")

        monkeypatch.setattr(disp, "_find_claude", boom)
        seen = {}

        async def fake_run_checks(config, **kw):
            seen["probe"] = kw["agent_probe"]
            return [nc.Check("node_id", True, "node-a")]

        monkeypatch.setattr(nc, "run_checks", fake_run_checks)
        cfg = tmp_path / "orchestrator.yaml"
        cfg.write_text(
            'version: "1"\nnode_id: "node-a"\n'
            f'paths: {{state_db: "{tmp_path}/s.db", worktrees: "{tmp_path}/w", '
            f'bare_repos: "{tmp_path}/r", contexts: "{tmp_path}/c", skills: "{tmp_path}/k"}}\n'
            f'transport: {{type: file_mock, file_mock: {{inbox: "{tmp_path}/i", '
            f'outbox: "{tmp_path}/o"}}}}\n'
        )
        result = CliRunner().invoke(app, ["node", "check", "--no-agent", "-c", str(cfg)])
        assert result.exit_code == 0, result.output
        assert seen["probe"] is None



class TestColdReviewFixes:
    @pytest.mark.asyncio
    async def test_a_refused_peer_is_reported_as_unreachable_through_the_real_fetch(
        self,
    ) -> None:
        import socket

        from ctrlrelay.web.fleet import fetch_peer

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]

        async def login() -> str:
            return "a"

        url = f"http://127.0.0.1:{port}"
        checks = await run_checks(
            SimpleNamespace(node_id="node-a", fleet=SimpleNamespace(
                peers=[url], timeout_seconds=2)),
            github_login=login, agent_probe=None, fetch=fetch_peer,
        )
        [peer] = [c for c in checks if c.name == f"peer {url}"]
        assert peer.ok is False
        assert "not with a ctrlrelay status page" not in peer.detail
        assert "Check the URL" in peer.detail

    def test_a_late_answer_after_the_loop_closed_is_dropped_quietly(self) -> None:
        import asyncio
        import threading

        from ctrlrelay.core.node_check import fetch_detached

        release = threading.Event()
        finished = threading.Event()
        errors = []
        old_hook = threading.excepthook
        threading.excepthook = lambda args: errors.append(args.exc_value)

        def slow(url, *, timeout):
            release.wait(10)
            try:
                return {}
            finally:
                finished.set()

        async def main():
            with pytest.raises(TimeoutError):
                await fetch_detached(slow, "http://x", timeout=0.05)

        try:
            asyncio.run(main())
            release.set()
            assert finished.wait(10)
            for t in threading.enumerate():
                if t.name == "ctrlrelay-peer-check":
                    t.join(5)
        finally:
            threading.excepthook = old_hook
        assert errors == []
