"""The GitHub API budget (#237).

``RATE_LIMIT`` has the shape of a real ``gh api rate_limit`` answer,
taken on 31 Oct 2026 and trimmed to the three buckets read here.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ctrlrelay.core.github_budget import (
    LOW,
    OK,
    OUT,
    Bucket,
    BudgetReading,
    BudgetStore,
    GitHubBudget,
    bucket_for,
    budget_json,
    parse_rate_limit,
)

NOW = 1_793_490_000


def rate_limit(core=4936, search=29, graphql=4599, reset=NOW + 3000) -> str:
    return json.dumps({"resources": {
        "core": {"limit": 5000, "remaining": core, "reset": reset, "used": 5000 - core},
        "search": {"limit": 30, "remaining": search, "reset": reset, "used": 30 - search},
        "graphql": {"limit": 5000, "remaining": graphql, "reset": reset, "used": 5000 - graphql},
    }})


class TestParsing:
    def test_reads_every_bucket(self) -> None:
        r = parse_rate_limit(rate_limit(), now=NOW)
        assert r.buckets["core"] == Bucket(4936, 5000, NOW + 3000)
        assert r.buckets["search"].limit == 30
        assert r.read_at == NOW

    @pytest.mark.parametrize("text", [
        "", "nope", "{}", '{"resources": {}}',
        json.dumps({"resources": {"core": {"limit": "5000", "remaining": 1, "reset": 1}}}),
        json.dumps({"resources": {"core": {"limit": True, "remaining": 1, "reset": 1}}}),
    ])
    def test_unreadable_is_none_never_a_full_or_empty_budget(self, text: str) -> None:
        assert parse_rate_limit(text, now=NOW) is None

    def test_unknown_is_published_as_unknown(self) -> None:
        assert budget_json(None, low_at=0.2, now=NOW) == "unknown"


class TestState:
    def test_ok_low_out(self) -> None:
        assert Bucket(4000, 5000, NOW + 60).state(0.2, NOW) == OK
        assert Bucket(999, 5000, NOW + 60).state(0.2, NOW) == LOW
        assert Bucket(0, 5000, NOW + 60).state(0.2, NOW) == OUT

    def test_after_the_reset_an_old_reading_is_not_out(self) -> None:
        assert Bucket(0, 5000, NOW + 60).state(0.2, NOW + 60) == OK


class TestWhichBucket:
    @pytest.mark.parametrize("args,bucket", [
        (("api", "rate_limit"), "core"),
        (("api", "/repos/o/r/issues/1/events", "--jq", "."), "core"),
        (("api", "--paginate", "repos/o/r/issues/1/comments"), "core"),
        (("api", "graphql", "-f", "query=..."), "graphql"),
        (("api", "search/issues?q=x"), "search"),
        (("search", "issues", "x"), "search"),
        (("issue", "view", "1", "--repo", "o/r"), "graphql"),
        (("pr", "list", "--repo", "o/r"), "graphql"),
    ])
    def test_bucket(self, args, bucket) -> None:
        assert bucket_for(args) == bucket


class TestBudget:
    def _budget(self, tmp_path: Path, answers: list[str], said: list[str], now=None):
        clock = now or [float(NOW)]

        async def fetch() -> str:
            return answers.pop(0)

        async def notify(text: str) -> None:
            said.append(text)

        return GitHubBudget(
            BudgetStore(tmp_path / "b.json"), low_at=0.2, fetch=fetch,
            notify=notify, clock=lambda: clock[0],
        ), clock

    @pytest.mark.asyncio
    async def test_one_line_when_low_one_when_out_one_when_back(self, tmp_path: Path) -> None:
        said: list[str] = []
        budget, clock = self._budget(tmp_path, [
            rate_limit(core=4000), rate_limit(core=900), rate_limit(core=800),
            rate_limit(core=0), rate_limit(core=0), rate_limit(core=5000, reset=NOW + 7000),
        ], said)
        await budget.refresh()
        assert said == []
        await budget.refresh()
        await budget.refresh()
        assert len(said) == 1 and "core API budget low" in said[0]
        await budget.refresh()
        await budget.refresh()
        assert len(said) == 2 and "used up" in said[1] and "resets at" in said[1]
        assert budget.blocked_until("core") == NOW + 3000
        clock[0] = NOW + 3000
        await budget.refresh()
        assert len(said) == 3 and "back" in said[2]
        assert budget.blocked_until("core") is None

    @pytest.mark.asyncio
    async def test_a_failed_read_keeps_the_last_reading(self, tmp_path: Path) -> None:
        said: list[str] = []
        budget, _ = self._budget(tmp_path, [rate_limit(core=0), "garbage"], said)
        await budget.refresh()
        assert await budget.refresh() is None
        assert budget.blocked_until("core") == NOW + 3000

    @pytest.mark.asyncio
    async def test_the_reading_is_stored_for_the_page(self, tmp_path: Path) -> None:
        budget, _ = self._budget(tmp_path, [rate_limit(core=10)], [])
        await budget.refresh()
        stored = BudgetStore(tmp_path / "b.json").read()
        assert stored.buckets["core"].remaining == 10
        page = budget_json(stored, low_at=0.2, now=NOW)
        assert page["core"]["state"] == LOW
        assert page["core"]["resets_at"].endswith("Z")


class TestTheGhGate:
    def _budget_out(self, tmp_path: Path, bucket: str = "core") -> GitHubBudget:
        budget = GitHubBudget(
            BudgetStore(tmp_path / "b.json"), low_at=0.2,
            fetch=AsyncMock(), clock=lambda: NOW,
        )
        budget.reading = BudgetReading(
            buckets={
                "core": Bucket(0 if bucket == "core" else 4000, 5000, NOW + 600),
                "graphql": Bucket(0 if bucket == "graphql" else 4000, 5000, NOW + 600),
            },
            read_at=NOW,
        )
        return budget

    @pytest.mark.asyncio
    async def test_an_out_bucket_fails_at_once_as_rate_limited(self, tmp_path: Path) -> None:
        from ctrlrelay.core.github import GitHubCLI, GitHubError
        from ctrlrelay.core.network import GhFailureKind, classify_gh_failure

        gh = GitHubCLI(gh_binary="gh", budget=self._budget_out(tmp_path))
        with patch("asyncio.create_subprocess_exec") as spawn:
            with pytest.raises(GitHubError) as err:
                await gh._run_gh("api", "repos/o/r/issues/1/comments")
        spawn.assert_not_called()
        assert classify_gh_failure(str(err.value)).kind is GhFailureKind.RATE_LIMITED

    @pytest.mark.asyncio
    async def test_other_buckets_and_the_rate_limit_read_still_go_through(
        self, tmp_path: Path,
    ) -> None:
        from ctrlrelay.core.github import GitHubCLI

        gh = GitHubCLI(gh_binary="gh", budget=self._budget_out(tmp_path, "core"))
        proc = AsyncMock()
        proc.communicate.return_value = (b"{}", b"")
        proc.returncode = 0
        with patch("asyncio.create_subprocess_exec", return_value=proc) as spawn:
            await gh._run_gh("issue", "view", "1", "--repo", "o/r")  # graphql
            await gh.rate_limit()
        assert spawn.call_count == 2

    @pytest.mark.asyncio
    async def test_a_rate_limit_answer_marks_the_bucket_out(self, tmp_path: Path) -> None:
        from ctrlrelay.core.github import GitHubCLI, GitHubError

        budget = GitHubBudget(
            BudgetStore(tmp_path / "b.json"), low_at=0.2,
            fetch=AsyncMock(), clock=lambda: NOW,
        )
        budget.reading = parse_rate_limit(rate_limit(core=3000), now=NOW)
        gh = GitHubCLI(gh_binary="gh", budget=budget)
        proc = AsyncMock()
        proc.communicate.return_value = (b"", b"HTTP 403: API rate limit exceeded for user")
        proc.returncode = 1
        with patch("asyncio.create_subprocess_exec", return_value=proc):
            with pytest.raises(GitHubError):
                await gh._run_gh("api", "repos/o/r")
        assert budget.blocked_until("core") == NOW + 3000


class TestSlowingDown:
    @pytest.mark.asyncio
    async def test_the_poll_interval_scales_while_low(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        import ctrlrelay.core.poller as poller_mod

        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(poller_mod.asyncio, "sleep", fake_sleep)
        poller = MagicMock()
        poller.poll = AsyncMock(return_value=[])
        low = iter([False, True, True])
        await poller_mod.run_poll_loop(
            poller=poller, handler=AsyncMock(), interval=100, max_iterations=4,
            interval_scale=lambda: 3.0 if next(low) else 1.0,
        )
        assert slept == [100, 300, 300]

    @pytest.mark.asyncio
    async def test_a_scale_that_raises_counts_as_one(self, monkeypatch) -> None:
        from unittest.mock import MagicMock

        import ctrlrelay.core.poller as poller_mod

        slept: list[float] = []

        async def fake_sleep(s: float) -> None:
            slept.append(s)

        monkeypatch.setattr(poller_mod.asyncio, "sleep", fake_sleep)
        poller = MagicMock()
        poller.poll = AsyncMock(return_value=[])

        def broken() -> float:
            raise RuntimeError("no reading")

        await poller_mod.run_poll_loop(
            poller=poller, handler=AsyncMock(), interval=100, max_iterations=2,
            interval_scale=broken,
        )
        assert slept == [100]

    def test_the_waiting_line_skips_its_github_calls_while_low(self) -> None:
        import time as _time

        from ctrlrelay.web.server import WaitingRefresher

        tracker = AsyncMock()
        refresher = WaitingRefresher(tracker, interval_s=0.05, budget_is_low=lambda: True)
        refresher.start()
        _time.sleep(0.2)
        refresher.stop()
        tracker.refresh.assert_not_awaited()

        tracker = AsyncMock()
        refresher = WaitingRefresher(tracker, interval_s=0.05, budget_is_low=lambda: False)
        refresher.start()
        _time.sleep(0.2)
        refresher.stop()
        assert tracker.refresh.await_count >= 1


class TestTheStatusBlock:
    def test_unknown_then_numbers(self, tmp_path: Path) -> None:
        from ctrlrelay.core.config import Config
        from ctrlrelay.web.server import StatusServer

        config = Config.model_validate({
            "node_id": "node-a",
            "paths": {k: str(tmp_path / k) for k in
                      ("state_db", "worktrees", "bare_repos", "contexts", "skills")},
            "transport": {"type": "file_mock", "file_mock": {
                "inbox": str(tmp_path / "in"), "outbox": str(tmp_path / "out"),
            }},
        })
        server = StatusServer.__new__(StatusServer)
        server.config = config
        assert server.node()["github_api"] == "unknown"
        import time as _time

        now = int(_time.time())
        BudgetStore.beside(config.paths.state_db).write(
            parse_rate_limit(rate_limit(core=100, reset=now + 600), now=now),
        )
        block = server.node()["github_api"]
        assert block["core"]["remaining"] == 100
        assert block["core"]["state"] == LOW
