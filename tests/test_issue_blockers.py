"""An issue whose GitHub "blocked by" issues are still open is not started
(#310).

The fakes answer ``list_blocked_by`` from rows in the shape GitHub's
``GET /repos/{owner}/{repo}/issues/{n}/dependencies/blocked_by`` returns: an
array of full issue objects, whose ``repository_url`` names the blocker's own
repository. The poll tests drive the real ``IssuePoller`` and
``run_poll_loop`` with the same gate ``handle_issue`` calls, so "comes back
after the blocker closes" and "survives a restart" are about the real
seen-set and state file, not a model of them.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ctrlrelay.core.blockers import hold_if_blocked
from ctrlrelay.core.github import GitHubCLI, GitHubError
from ctrlrelay.core.poller import (
    BLOCKERS_UNREADABLE_AFTER,
    IssuePoller,
    run_poll_loop,
)
from tests._poller_fakes import wire_search

REPO = "owner/repo"
OTHER = "owner/other"


def _issue_row(repo: str, number: int, state: str) -> dict[str, Any]:
    """One element of the blocked_by array, as GitHub sends it (abridged
    only in the user object)."""
    return {
        "id": 1000 + number,
        "node_id": f"I_kwDO{number}",
        "url": f"https://api.github.com/repos/{repo}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "labels_url": f"https://api.github.com/repos/{repo}/issues/{number}/labels{{/name}}",
        "comments_url": f"https://api.github.com/repos/{repo}/issues/{number}/comments",
        "events_url": f"https://api.github.com/repos/{repo}/issues/{number}/events",
        "html_url": f"https://github.com/{repo}/issues/{number}",
        "number": number,
        "state": state,
        "state_reason": "completed" if state == "closed" else None,
        "title": f"blocker {number}",
        "body": "",
        "user": {"login": "alice", "id": 1, "type": "User"},
        "labels": [],
        "assignee": None,
        "assignees": [],
        "milestone": None,
        "locked": False,
        "comments": 0,
        "closed_at": "2026-11-01T00:00:00Z" if state == "closed" else None,
        "created_at": "2026-10-01T00:00:00Z",
        "updated_at": "2026-11-01T00:00:00Z",
        "author_association": "OWNER",
    }


def _held(blockers: list[str] | None, failures: int = 0) -> dict[str, Any]:
    """A poller ``blocked`` entry."""
    return {"open": blockers, "failures": failures}


def _as_jq_lines(rows: list[dict[str, Any]]) -> str:
    """What ``gh api --paginate ... --jq '.[] | tojson'`` prints for
    ``rows``: one compact JSON object per line."""
    return "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows)


class TestListBlockedBy:
    @pytest.mark.asyncio
    async def test_reads_the_real_shape_including_another_repo(self) -> None:
        rows = [_issue_row(REPO, 5, "open"), _issue_row(OTHER, 9, "closed")]
        with patch.object(
            GitHubCLI, "_run_gh", AsyncMock(return_value=_as_jq_lines(rows)),
        ) as run:
            got = await GitHubCLI().list_blocked_by(REPO, 12)
        assert got == [
            {"repo": REPO, "number": 5, "state": "open"},
            {"repo": OTHER, "number": 9, "state": "closed"},
        ]
        args = run.call_args.args
        assert args[:3] == (
            "api", "--paginate", f"/repos/{REPO}/issues/12/dependencies/blocked_by",
        )
        assert args[3:] == ("--jq", ".[] | tojson")

    @pytest.mark.asyncio
    async def test_none_is_an_empty_list(self) -> None:
        with patch.object(GitHubCLI, "_run_gh", AsyncMock(return_value="")):
            assert await GitHubCLI().list_blocked_by(REPO, 12) == []

    @pytest.mark.asyncio
    async def test_falls_back_to_html_url_for_the_repository(self) -> None:
        row = _issue_row(OTHER, 9, "open")
        del row["repository_url"]
        with patch.object(
            GitHubCLI, "_run_gh", AsyncMock(return_value=_as_jq_lines([row])),
        ):
            got = await GitHubCLI().list_blocked_by(REPO, 12)
        assert got == [{"repo": OTHER, "number": 9, "state": "open"}]

    @pytest.mark.parametrize("broken", [
        {"state": None},
        {"state": "merged"},
        {"number": "5"},
        {"repository_url": None, "html_url": None},
    ])
    @pytest.mark.asyncio
    async def test_an_unreadable_row_raises_rather_than_being_dropped(
        self, broken: dict[str, Any],
    ) -> None:
        row = {**_issue_row(REPO, 5, "open"), **broken}
        with patch.object(
            GitHubCLI, "_run_gh", AsyncMock(return_value=_as_jq_lines([row])),
        ):
            with pytest.raises(GitHubError):
                await GitHubCLI().list_blocked_by(REPO, 12)

    @pytest.mark.asyncio
    async def test_is_held_by_the_github_budget_gate(self) -> None:
        """A REST call: when the ``core`` budget is used up it fails at
        once as rate limited, without running ``gh``."""
        budget = MagicMock()
        budget.blocked_until = MagicMock(return_value=2_000_000_000)
        gh = GitHubCLI(gh_binary="/nonexistent/gh", budget=budget)
        with pytest.raises(GitHubError, match="rate limit"):
            await gh.list_blocked_by(REPO, 12)
        budget.blocked_until.assert_called_once_with("core")


class _Blockers:
    """A fake ``list_blocked_by``: per issue, the blocker rows; or an
    exception to raise. Runs each row through the real reader so the
    fake cannot accept a shape the real method would refuse."""

    def __init__(self) -> None:
        self.rows: dict[int, list[dict[str, Any]]] = {}
        self.error: Exception | None = None

    async def __call__(self, repo: str, number: int) -> list[dict[str, Any]]:
        if self.error is not None:
            raise self.error
        with patch.object(
            GitHubCLI, "_run_gh",
            AsyncMock(return_value=_as_jq_lines(self.rows.get(number, []))),
        ):
            return await GitHubCLI().list_blocked_by(repo, number)


def _github(blockers: _Blockers) -> MagicMock:
    """GitHub with one open issue, #12, self-assigned to alice."""
    gh = MagicMock()
    issue = {"number": 12, "title": "blocked one", "state": "open",
             "labels": [], "assignees": [{"login": "alice"}]}
    gh.list_assigned_issues = AsyncMock(return_value=[issue])
    gh.list_issues_by_label = AsyncMock(return_value=[])
    gh.list_assignment_events = AsyncMock(return_value=[{
        "event": "assigned", "actor": {"login": "alice"},
        "assignee": {"login": "alice"}, "assigner": {"login": "alice"},
        "created_at": "2026-01-01T00:00:00Z",
    }])
    gh.get_issue_gate = AsyncMock(return_value={
        "state": "open", "labels": [], "assignees": ["alice"],
    })
    gh.repo_is_archived = AsyncMock(return_value=False)
    gh.list_blocked_by = blockers
    return wire_search(gh)


def _poller(gh: MagicMock, state_file: Path) -> IssuePoller:
    return IssuePoller(
        github=gh, username="alice", repos=[REPO], state_file=state_file,
    )


class _Relay:
    """What ``handle_issue`` does up to the claim: the blocker gate, then
    "start". Records starts and chat lines."""

    def __init__(self, gh: MagicMock, poller: IssuePoller) -> None:
        self.gh = gh
        self.poller = poller
        self.started: list[int] = []
        self.chat: list[str] = []

    async def handle(self, repo: str, issue: dict[str, Any]) -> None:
        hold = await hold_if_blocked(self.gh, self.poller, repo, issue["number"])
        if hold is not None:
            if hold.chat is not None:
                self.chat.append(hold.chat)
            return
        self.started.append(issue["number"])

    async def poll(self) -> None:
        await run_poll_loop(self.poller, self.handle, interval=0, max_iterations=1)


@pytest.mark.asyncio
async def test_no_blockers_starts(tmp_path: Path) -> None:
    blockers = _Blockers()
    gh = _github(blockers)
    relay = _Relay(gh, _poller(gh, tmp_path / "s.json"))
    await relay.poll()
    assert relay.started == [12]
    assert relay.chat == []


@pytest.mark.asyncio
async def test_all_blockers_closed_starts(tmp_path: Path) -> None:
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "closed"), _issue_row(OTHER, 9, "closed")]
    gh = _github(blockers)
    relay = _Relay(gh, _poller(gh, tmp_path / "s.json"))
    await relay.poll()
    assert relay.started == [12]


@pytest.mark.asyncio
async def test_an_open_blocker_holds_the_issue_and_is_logged(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "open"), _issue_row(REPO, 6, "closed")]
    gh = _github(blockers)
    poller = _poller(gh, tmp_path / "s.json")
    relay = _Relay(gh, poller)
    with caplog.at_level("INFO"):
        await relay.poll()
    assert relay.started == []
    assert poller.blocked == {REPO: {12: _held(["#5"])}}
    assert 12 not in poller.seen_issues.get(REPO, set())
    logged = [r for r in caplog.records if r.getMessage() == "poll.issue.blocked"]
    assert [getattr(r, "blocked_by", None) for r in logged] == [["#5"]]
    assert relay.chat and "blocked by #5" in relay.chat[0]


@pytest.mark.asyncio
async def test_a_blocker_in_another_repository_holds_it(tmp_path: Path) -> None:
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(OTHER, 9, "open")]
    gh = _github(blockers)
    poller = _poller(gh, tmp_path / "s.json")
    relay = _Relay(gh, poller)
    await relay.poll()
    assert relay.started == []
    assert poller.blocked == {REPO: {12: _held([f"{OTHER}#9"])}}
    assert f"blocked by {OTHER}#9" in relay.chat[0]


@pytest.mark.parametrize("error", [
    GitHubError("gh failed: HTTP 404: Not Found"),
    GitHubError("gh failed: API rate limit exceeded"),
    TimeoutError(),
])
@pytest.mark.asyncio
async def test_a_failed_lookup_does_not_start_and_is_retried(
    tmp_path: Path, error: Exception, caplog: pytest.LogCaptureFixture,
) -> None:
    blockers = _Blockers()
    blockers.error = error
    gh = _github(blockers)
    poller = _poller(gh, tmp_path / "s.json")
    relay = _Relay(gh, poller)
    with caplog.at_level("INFO"):
        await relay.poll()
    assert relay.started == []
    assert poller.blocked == {REPO: {12: _held(None, failures=1)}}
    assert any(r.getMessage() == "poll.issue.blockers_unknown"
               for r in caplog.records)
    # One failure is not worth a chat line; it is said once it persists.
    assert relay.chat == []
    for _ in range(BLOCKERS_UNREADABLE_AFTER - 1):
        await relay.poll()
    assert relay.started == []
    assert len(relay.chat) == 1 and "could not be read" in relay.chat[0]
    await relay.poll()
    assert len(relay.chat) == 1

    # The next poll asks again, and starts it once the answer is "none open".
    blockers.error = None
    await relay.poll()
    assert relay.started == [12]
    assert poller.blocked == {}


@pytest.mark.asyncio
async def test_starts_on_the_first_poll_after_the_blocker_closes(
    tmp_path: Path,
) -> None:
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "open")]
    gh = _github(blockers)
    poller = _poller(gh, tmp_path / "s.json")
    relay = _Relay(gh, poller)

    await relay.poll()
    await relay.poll()
    assert relay.started == []

    blockers.rows[12] = [_issue_row(REPO, 5, "closed")]
    await relay.poll()
    assert relay.started == [12]
    assert poller.blocked == {}
    assert 12 in poller.seen_issues[REPO]


@pytest.mark.asyncio
async def test_the_chat_hears_it_once_not_every_poll(tmp_path: Path) -> None:
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "open")]
    gh = _github(blockers)
    relay = _Relay(gh, _poller(gh, tmp_path / "s.json"))

    for _ in range(3):
        await relay.poll()
    # A second blocker opening is not news either.
    blockers.rows[12] = [_issue_row(REPO, 5, "open"), _issue_row(OTHER, 9, "open")]
    await relay.poll()
    assert len(relay.chat) == 1

    # "Could not read", once it has persisted, is a different reason.
    blockers.error = GitHubError("gh failed: HTTP 502")
    for _ in range(BLOCKERS_UNREADABLE_AFTER + 2):
        await relay.poll()
    assert len(relay.chat) == 2


@pytest.mark.asyncio
async def test_one_failed_lookup_between_the_same_blockers_says_nothing(
    tmp_path: Path,
) -> None:
    """blocked -> one failed lookup -> blocked by the same: one chat line
    in all, not three."""
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "open")]
    gh = _github(blockers)
    poller = _poller(gh, tmp_path / "s.json")
    relay = _Relay(gh, poller)

    await relay.poll()
    blockers.error = GitHubError("gh failed: HTTP 502")
    await relay.poll()
    # The last blockers read are kept through the blip.
    assert poller.blocked == {REPO: {12: _held(["#5"], failures=1)}}
    blockers.error = None
    await relay.poll()
    assert len(relay.chat) == 1
    assert poller.blocked == {REPO: {12: _held(["#5"])}}
    assert relay.started == []


@pytest.mark.asyncio
async def test_recovering_after_a_long_outage(tmp_path: Path) -> None:
    """Once the chat was told the blockers cannot be read, finding the same
    blockers again is silent, and finding different ones is said."""
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "open")]
    gh = _github(blockers)
    relay = _Relay(gh, _poller(gh, tmp_path / "s.json"))

    await relay.poll()
    blockers.error = GitHubError("gh failed: HTTP 502")
    for _ in range(BLOCKERS_UNREADABLE_AFTER):
        await relay.poll()
    assert len(relay.chat) == 2
    blockers.error = None
    await relay.poll()
    assert len(relay.chat) == 2

    blockers.error = GitHubError("gh failed: HTTP 502")
    for _ in range(BLOCKERS_UNREADABLE_AFTER):
        await relay.poll()
    assert len(relay.chat) == 3
    blockers.error = None
    blockers.rows[12] = [_issue_row(OTHER, 9, "open")]
    await relay.poll()
    assert len(relay.chat) == 4 and f"blocked by {OTHER}#9" in relay.chat[3]


@pytest.mark.asyncio
async def test_a_failed_events_call_while_held_does_not_lose_the_issue(
    tmp_path: Path,
) -> None:
    """A held assignment-only issue is offered every poll. A failed
    self-assignment (events) call marks an issue seen for good, so the
    poll does not ask again for an issue already held: it was confirmed
    when it was first held."""
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "open")]
    gh = _github(blockers)
    poller = _poller(gh, tmp_path / "s.json")
    relay = _Relay(gh, poller)

    await relay.poll()
    assert relay.started == []
    gh.list_assignment_events = AsyncMock(
        side_effect=GitHubError("gh failed: HTTP 502"),
    )
    await relay.poll()
    assert relay.started == []

    blockers.rows[12] = [_issue_row(REPO, 5, "closed")]
    await relay.poll()
    assert relay.started == [12]


@pytest.mark.asyncio
async def test_a_held_issue_survives_a_restart(tmp_path: Path) -> None:
    """The poll marks an issue seen before handing it over; a held issue
    left in the seen-set would never be offered again. After a restart
    (a new poller on the same state file, not a first run, so no seed) it
    is still offered, the chat is not told twice, and it starts once the
    blocker closes."""
    state_file = tmp_path / "poller_state.json"
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "open")]
    gh = _github(blockers)
    first = _Relay(gh, _poller(gh, state_file))
    await first.poll()
    assert first.started == [] and len(first.chat) == 1

    on_disk = json.loads(state_file.read_text())
    assert 12 not in on_disk["seen_issues"].get(REPO, [])
    assert on_disk["blocked"] == {REPO: {"12": _held(["#5"])}}

    second = _Relay(gh, _poller(gh, state_file))
    assert second.poller.blocked == {REPO: {12: _held(["#5"])}}
    await second.poll()
    assert second.started == []
    assert second.chat == []

    blockers.rows[12] = [_issue_row(REPO, 5, "closed")]
    await second.poll()
    assert second.started == [12]


@pytest.mark.asyncio
async def test_closing_the_held_issue_forgets_the_hold(tmp_path: Path) -> None:
    blockers = _Blockers()
    blockers.rows[12] = [_issue_row(REPO, 5, "open")]
    gh = _github(blockers)
    poller = _poller(gh, tmp_path / "s.json")
    relay = _Relay(gh, poller)
    await relay.poll()
    assert poller.blocked == {REPO: {12: _held(["#5"])}}

    # Closed between the poll and its turn: the re-read drops it.
    gh.get_issue_gate = AsyncMock(return_value={
        "state": "closed", "labels": [], "assignees": ["alice"],
    })
    await relay.poll()
    assert poller.blocked == {}
    assert relay.started == []


class TestHandleIssueCallsTheGate:
    """``handle_issue`` is a closure inside ``poller_start``; the tests
    above drive the gate it calls. This pins that it is called, and before
    the claim look and either pipeline."""

    def test_the_gate_comes_before_the_claim_and_the_run(self) -> None:
        import ctrlrelay.cli as cli_module

        tree = ast.parse(Path(cli_module.__file__).read_text())
        handle_issue = next(
            n for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_issue"
        )

        def first_call(name: str) -> int:
            lines = [
                n.lineno for n in ast.walk(handle_issue)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == name
            ]
            assert lines, f"handle_issue no longer calls {name}"
            return min(lines)

        gate = first_call("hold_if_blocked")
        assert gate < first_call("peek_claim_owner")
        assert gate < first_call("connect_notify_transport")
        assert gate < first_call("run_dev_issue")
        assert gate < first_call("run_task_issue")
