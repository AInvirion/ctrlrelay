"""Tests for the status page's waiting line (#209).

The issue dicts are a real `gh search issues --json
repository,number,title,labels,assignees,updatedAt` response, captured and
then anonymised (repository names, logins, ids and titles replaced, shape
untouched), so the code is exercised against the shape GitHub actually
returns, not a guess at it.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ctrlrelay.core.config import Config
from ctrlrelay.core.github import GitHubCLI, GitHubError
from ctrlrelay.core.poller import IssuePoller
from ctrlrelay.web.waiting import (
    VIA_LABEL,
    WaitingItem,
    WaitingTracker,
    compute_waiting,
    load_seen,
)

FIXTURE = Path(__file__).parent / "fixtures" / "gh_search_issues.json"

ALPHA = "example-org/alpha"
BETA = "example-org/beta"
GAMMA = "other-org/gamma"


def _raw() -> list[dict]:
    return json.loads(FIXTURE.read_text())


def _issues() -> list[dict]:
    """The fixture as `search_assigned_issues` returns it (with `repo`)."""
    out = []
    for issue in _raw():
        issue = copy.deepcopy(issue)
        issue["repo"] = issue["repository"]["nameWithOwner"]
        out.append(issue)
    return out


def _config(tmp_path: Path, repos: list[dict]) -> Config:
    return Config.model_validate({
        "node_id": "test-node",
        "paths": {
            "state_db": str(tmp_path / "state.db"),
            "worktrees": str(tmp_path / "wt"),
            "bare_repos": str(tmp_path / "bare"),
            "contexts": str(tmp_path / "ctx"),
            "skills": str(tmp_path / "skills"),
            "repo_root": str(tmp_path / "src"),
        },
        "transport": {
            "type": "file_mock",
            "file_mock": {
                "inbox": str(tmp_path / "in"),
                "outbox": str(tmp_path / "out"),
            },
        },
        "dashboard": {"enabled": False},
        "repos": repos,
    })


def _accepting(name: str, **automation) -> dict:
    """A repo whose assignments need no confirmation, so reasons are plain."""
    return {
        "name": name,
        "automation": {"accept_foreign_assignments": True, **automation},
    }


def _issue_list_row(
    number: int, labels: list[str], *, assignees: list[str],
) -> dict:
    """A row in the shape `gh issue list --json` returns (what
    `list_issues_by_label` gives back): no `repository` key."""
    return {
        "number": number,
        "title": "...",
        "state": "OPEN",
        "body": "",
        "labels": [{"id": f"LA_{n}", "name": n, "description": "", "color": "ffffff"}
                   for n in labels],
        "assignees": [{"id": f"U_{a}", "login": a, "name": ""} for a in assignees],
        "createdAt": "2026-10-20T10:00:00Z",
        "updatedAt": "2026-10-21T10:00:00Z",
    }


def _keys(items: list[WaitingItem]) -> list[tuple[str, int]]:
    return [(i.repo, i.issue_number) for i in items]


def test_fixture_is_the_real_search_shape() -> None:
    """Guard on the fixture itself: if it were hand-written in the shape we
    expected, every test below would agree with our assumption for free."""
    raw = _raw()
    assert len(raw) == 5
    for issue in raw:
        assert set(issue) == {
            "assignees", "labels", "number", "repository", "title", "updatedAt",
        }
        assert set(issue["repository"]) == {"name", "nameWithOwner"}
        assert all("name" in label for label in issue["labels"])
        assert all("login" in a for a in issue["assignees"])


@pytest.mark.asyncio
async def test_search_assigned_issues_runs_one_search_and_adds_repo() -> None:
    with patch.object(
        GitHubCLI, "_run_gh", new=AsyncMock(return_value=FIXTURE.read_text()),
    ) as run:
        issues = await GitHubCLI().search_assigned_issues("octo", limit=50)

    run.assert_awaited_once_with(
        "search", "issues",
        "--assignee", "octo",
        "--state", "open",
        "--limit", "50",
        "--json", "repository,number,title,labels,assignees,updatedAt",
    )
    assert [(i["repo"], i["number"]) for i in issues] == [
        (ALPHA, 304), (BETA, 48), (BETA, 47), (BETA, 46), (GAMMA, 366),
    ]


@pytest.mark.asyncio
async def test_search_assigned_issues_default_limit_is_the_search_maximum() -> None:
    with patch.object(
        GitHubCLI, "_run_gh", new=AsyncMock(return_value="[]"),
    ) as run:
        await GitHubCLI().search_assigned_issues("octo")

    args = run.await_args.args
    assert args[args.index("--limit") + 1] == "1000"


@pytest.mark.asyncio
async def test_current_login_asks_gh_api_user() -> None:
    with patch.object(
        GitHubCLI, "_run_gh", new=AsyncMock(return_value="octo\n"),
    ) as run:
        login = await GitHubCLI().current_login()

    run.assert_awaited_once_with("api", "user", "--jq", ".login")
    assert login == "octo"


@pytest.mark.asyncio
async def test_current_login_empty_is_an_error() -> None:
    with patch.object(GitHubCLI, "_run_gh", new=AsyncMock(return_value="\n")):
        with pytest.raises(GitHubError):
            await GitHubCLI().current_login()


def test_only_configured_repos(tmp_path: Path) -> None:
    config = _config(tmp_path, [_accepting(GAMMA)])

    items = compute_waiting(config, _issues(), seen=set(), running=[], locks={})

    assert _keys(items) == [(GAMMA, 366)]
    assert items[0].title == "..."


def test_label_rules_match_the_poller(tmp_path: Path) -> None:
    """Every fixture issue is kept or dropped exactly as the poller's own
    static label checks decide, on the same input."""
    exclude = ["epic"]          # drops the three beta issues
    require = ["ready"]         # no fixture issue carries it
    include = ["ctrlrelay:auto"]  # admits ALPHA#304 despite require
    config = _config(tmp_path, [
        _accepting(ALPHA, require_labels=require, include_labels=include),
        _accepting(BETA, exclude_labels=exclude),
        _accepting(GAMMA, require_labels=require),
    ])
    rules = {
        ALPHA: (set(), {"ready"}, {"ctrlrelay:auto"}),
        BETA: ({"epic"}, set(), set()),
        GAMMA: (set(), {"ready"}, set()),
    }

    expected = []
    for issue in _issues():
        ex, req, inc = rules[issue["repo"]]
        if IssuePoller._matched_exclude_label(issue, ex) is not None:
            continue
        label_match = IssuePoller._matched_include_label(issue, inc)
        assigned = not req or IssuePoller._matched_require_label(issue, req)
        if label_match is None and not assigned:
            continue
        expected.append((issue["repo"], issue["number"]))

    items = compute_waiting(config, _issues(), seen=set(), running=[], locks={})

    assert expected == [(ALPHA, 304)]  # the rules above really do discriminate
    assert _keys(items) == expected


def test_default_exclude_labels_apply(tmp_path: Path) -> None:
    """A repo with no automation block still gets the poller's default
    exclude labels, because both read the same config model."""
    config = _config(tmp_path, [{"name": GAMMA}])
    assert "automation" not in config.repos[0].model_fields_set
    issues = [i for i in _issues() if i["repo"] == GAMMA]
    # Present case first, so the drop below is the label's doing.
    assert _keys(compute_waiting(
        config, issues, seen=set(), running=[], locks={},
    )) == [(GAMMA, 366)]

    issues[0]["labels"].append({"name": "Manual"})

    assert compute_waiting(config, issues, seen=set(), running=[], locks={}) == []


def test_assignment_only_issue_uses_the_self_assignment_answer(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path, [
        {"name": GAMMA},
        _accepting(BETA),
        {"name": ALPHA, "automation": {"include_labels": ["ctrlrelay:auto"]}},
    ])

    def reasons(assignments):
        items = compute_waiting(
            config, _issues(), seen=set(), running=[], locks={},
            assignments=assignments,
        )
        return {(i.repo, i.issue_number): i.reason for i in items}

    # No answer (the check failed): says what it waits on.
    unknown = reasons({})
    assert unknown[(GAMMA, 366)] == "waits for the poller to confirm the assignment"
    # An accepting repo and a label-admitted issue are never checked.
    assert unknown[(BETA, 48)] == "next poll"
    assert unknown[(ALPHA, 304)] == "next poll"

    # Self-assigned: the real reason.
    assert reasons({f"{GAMMA}#366": True})[(GAMMA, 366)] == "next poll"
    # Assigned by someone else: the poller will not take it.
    assert (GAMMA, 366) not in reasons({f"{GAMMA}#366": False})
    assert (BETA, 48) in reasons({f"{GAMMA}#366": False})


def test_label_only_issue_is_admitted_by_its_label(tmp_path: Path) -> None:
    """A row from a label query, assigned to nobody, is admitted by the
    include label alone; one without a matching label and not assigned to
    the operator is not (the poller's defensive "neither trigger" case)."""
    config = _config(tmp_path, [
        {"name": GAMMA, "automation": {"include_labels": ["ctrlrelay:auto"]}},
    ])
    labelled = _issue_list_row(1, ["ctrlrelay:auto"], assignees=[])
    stray = _issue_list_row(2, ["bug"], assignees=[])

    items = compute_waiting(
        config, [{**labelled, "repo": GAMMA, VIA_LABEL: True},
                 {**stray, "repo": GAMMA, VIA_LABEL: True}],
        seen=set(), running=[], locks={}, username="octo",
    )

    assert [(i.issue_number, i.reason) for i in items] == [(1, "next poll")]


def test_seen_and_running_issues_are_dropped(tmp_path: Path) -> None:
    config = _config(tmp_path, [_accepting(BETA)])
    # Present-case first: all three are waiting with nothing seen or running.
    assert _keys(compute_waiting(
        config, _issues(), seen=set(), running=[], locks={},
    )) == [(BETA, 48), (BETA, 47), (BETA, 46)]

    items = compute_waiting(
        config, _issues(),
        seen={f"{BETA}#48"},
        running=[{"session_id": "s1", "repo": BETA, "issue_number": 47}],
        locks={},
    )

    assert _keys(items) == [(BETA, 46)]


def test_load_seen_reads_the_poller_state_file(tmp_path: Path) -> None:
    """Written by the real poller, read back in our key format."""
    state_file = tmp_path / "poller_state.json"
    poller = IssuePoller(
        github=GitHubCLI(), username="octo", repos=[BETA], state_file=state_file,
    )
    poller.mark_seen(BETA, 48)
    poller.mark_seen(GAMMA, 366)

    assert load_seen(state_file) == {f"{BETA}#48", f"{GAMMA}#366"}


def test_load_seen_missing_or_corrupt_is_empty(tmp_path: Path) -> None:
    assert load_seen(tmp_path / "nope.json") == set()
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert load_seen(bad) == set()
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"seen_issues": ["x"]}))
    assert load_seen(wrong) == set()


def test_reasons(tmp_path: Path) -> None:
    config = _config(tmp_path, [_accepting(BETA), _accepting(GAMMA)])
    running = [
        {"session_id": "s-215", "repo": BETA, "issue_number": 215},
        {"session_id": "s-9", "repo": "other/repo", "issue_number": 9},
    ]

    items = compute_waiting(
        config, _issues(), seen=set(), running=running,
        locks={BETA: "s-215", "other/repo": "s-9"},
    )
    reasons = {(i.repo, i.issue_number): i.reason for i in items}

    assert reasons[(BETA, 48)] == "same repo as #215"
    # Not locked: busy with the first running session.
    assert reasons[(GAMMA, 366)] == f"poller busy with {BETA}#215"

    idle = compute_waiting(config, _issues(), seen=set(), running=[], locks={})
    assert {i.reason for i in idle} == {"next poll"}


def test_reasons_for_a_session_without_an_issue(tmp_path: Path) -> None:
    """A secops session holds a repo lock with no issue number."""
    config = _config(tmp_path, [_accepting(BETA), _accepting(GAMMA)])
    running = [{"session_id": "sec", "repo": BETA, "issue_number": None,
                "pipeline": "secops"}]

    items = compute_waiting(
        config, _issues(), seen=set(), running=running, locks={BETA: "sec"},
    )
    reasons = {(i.repo, i.issue_number): i.reason for i in items}

    assert reasons[(BETA, 48)] == "same repo as a running secops session"
    assert reasons[(GAMMA, 366)] == f"poller busy with {BETA} (secops)"


def test_order_is_config_repo_order_then_github_order(tmp_path: Path) -> None:
    config = _config(tmp_path, [_accepting(GAMMA), _accepting(BETA), _accepting(ALPHA)])

    items = compute_waiting(config, _issues(), seen=set(), running=[], locks={})

    assert _keys(items) == [
        (GAMMA, 366), (BETA, 48), (BETA, 47), (BETA, 46), (ALPHA, 304),
    ]
    assert items[0].updated_ts == 1793043904  # 2026-10-26T19:45:04Z


class _FakeGitHub:
    def __init__(
        self,
        results: list,
        *,
        events: dict | None = None,
        by_label: dict | None = None,
    ) -> None:
        self.results = list(results)
        self.logins = 0
        self.searches: list[str] = []
        self.limits: list[int] = []
        # (repo, number) -> events list, or an Exception to raise
        self.events = events or {}
        self.event_calls: list[tuple[str, int]] = []
        # (repo, label) -> rows
        self.by_label = by_label or {}
        self.label_calls: list[tuple[str, str]] = []

    async def current_login(self) -> str:
        self.logins += 1
        return "octo"

    async def search_assigned_issues(self, assignee: str, *, limit: int = 1000):
        self.searches.append(assignee)
        self.limits.append(limit)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    async def list_assignment_events(self, repo: str, issue_number: int):
        self.event_calls.append((repo, issue_number))
        result = self.events[(repo, issue_number)]
        if isinstance(result, Exception):
            raise result
        return result

    async def list_issues_by_label(self, repo: str, label: str, **_):
        self.label_calls.append((repo, label))
        return [copy.deepcopy(r) for r in self.by_label.get((repo, label), [])]


def _assigned_by(actor: str) -> list[dict]:
    """`list_assignment_events` output: GitHub `assigned` issue events."""
    return [{
        "event": "assigned",
        "actor": {"login": actor},
        "assignee": {"login": "octo"},
        "created_at": "2026-10-20T10:00:00Z",
    }]


def _tracker(
    tmp_path: Path, gh: _FakeGitHub, clock: list[int],
    repos: list[dict] | None = None,
    **kwargs,
) -> WaitingTracker:
    config = _config(tmp_path, repos or [_accepting(BETA)])
    return WaitingTracker(
        config, gh,  # type: ignore[arg-type]
        sessions=lambda: ([], {}),
        clock=lambda: clock[0],
        **kwargs,
    )


@pytest.mark.asyncio
async def test_gh_failure_keeps_last_good(tmp_path: Path) -> None:
    two = [i for i in _issues() if i["number"] in (48, 47)]
    gh = _FakeGitHub([two, GitHubError("gh failed: API rate limit exceeded")])
    clock = [1_800_000_000]
    tracker = _tracker(tmp_path, gh, clock)

    await tracker.refresh()
    first = tracker.snapshot()
    assert first["status"] == "ok"
    assert len(first["items"]) == 2
    assert first["last_good_ts"] == 1_800_000_000
    assert first["error"] is None

    clock[0] += 60
    await tracker.refresh()
    snap = tracker.snapshot()

    assert snap["status"] == "unknown"
    assert snap["items"] == first["items"]
    assert [i["issue_number"] for i in snap["items"]] == [48, 47]
    assert snap["last_good_ts"] == 1_800_000_000
    assert snap["last_good"] == "2027-01-15T08:00:00+00:00"
    assert snap["checked_ts"] == 1_800_000_060
    assert "rate limit" in snap["error"]
    assert gh.searches == ["octo", "octo"]


@pytest.mark.asyncio
async def test_never_succeeded_is_unknown_not_empty(tmp_path: Path) -> None:
    gh = _FakeGitHub([GitHubError("gh failed: not logged in")])
    tracker = _tracker(tmp_path, gh, [1_800_000_000])

    before = tracker.snapshot()
    assert before["status"] == "unknown"
    assert before["error"]

    await tracker.refresh()
    snap = tracker.snapshot()

    assert snap["status"] == "unknown"
    assert snap["items"] == []
    assert snap["last_good"] is None
    assert snap["last_good_ts"] is None
    assert "not logged in" in snap["error"]


@pytest.mark.asyncio
async def test_login_failure_is_retried_on_next_refresh(tmp_path: Path) -> None:
    class _FlakyLogin(_FakeGitHub):
        async def current_login(self) -> str:
            self.logins += 1
            if self.logins == 1:
                raise GitHubError("gh failed: timeout")
            return "octo"

    gh = _FlakyLogin([[], []])
    tracker = _tracker(tmp_path, gh, [1_800_000_000])

    await tracker.refresh()
    assert tracker.snapshot()["status"] == "unknown"
    assert gh.searches == []

    await tracker.refresh()
    assert tracker.snapshot()["status"] == "ok"
    assert gh.logins == 2

    # Once known, the login is not asked for again.
    await tracker.refresh()
    assert gh.logins == 2
    assert gh.searches == ["octo", "octo"]


@pytest.mark.asyncio
async def test_session_read_failure_is_unknown(tmp_path: Path) -> None:
    """If the sessions cannot be read we cannot tell running from waiting."""
    gh = _FakeGitHub([_issues()])
    config = _config(tmp_path, [_accepting(BETA)])

    def broken():
        raise OSError("database is locked")

    tracker = WaitingTracker(
        config, gh, sessions=broken, clock=lambda: 1_800_000_000,  # type: ignore[arg-type]
    )
    await tracker.refresh()
    snap = tracker.snapshot()

    assert snap["status"] == "unknown"
    assert "database is locked" in snap["error"]
    assert snap["items"] == []


@pytest.mark.asyncio
async def test_snapshot_items_carry_timestamp_pairs(tmp_path: Path) -> None:
    gh = _FakeGitHub([[i for i in _issues() if i["number"] == 48]])
    tracker = _tracker(tmp_path, gh, [1_800_000_000])

    await tracker.refresh()
    (item,) = tracker.snapshot()["items"]

    assert item == {
        "repo": BETA,
        "issue_number": 48,
        "title": "...",
        "reason": "next poll",
        "updated_ts": 1793294926,
        "updated": "2026-10-29T17:28:46+00:00",
    }


@pytest.mark.asyncio
async def test_self_assigned_answer_is_cached(tmp_path: Path) -> None:
    ri = [i for i in _issues() if i["repo"] == GAMMA]
    gh = _FakeGitHub(
        [ri, ri, ri], events={(GAMMA, 366): _assigned_by("octo")},
    )
    clock = [1_800_000_000]
    tracker = _tracker(tmp_path, gh, clock, repos=[{"name": GAMMA}])

    await tracker.refresh()
    assert [(i["issue_number"], i["reason"]) for i in tracker.snapshot()["items"]] \
        == [(366, "next poll")]
    assert gh.event_calls == [(GAMMA, 366)]

    clock[0] += 3600  # long past the foreign recheck window
    await tracker.refresh()
    await tracker.refresh()
    assert gh.event_calls == [(GAMMA, 366)]
    assert tracker.snapshot()["items"][0]["reason"] == "next poll"


@pytest.mark.asyncio
async def test_foreign_answer_is_rechecked_after_ten_minutes(tmp_path: Path) -> None:
    ri = [i for i in _issues() if i["repo"] == GAMMA]
    gh = _FakeGitHub(
        [ri, ri, ri], events={(GAMMA, 366): _assigned_by("someone-else")},
    )
    clock = [1_800_000_000]
    tracker = _tracker(tmp_path, gh, clock, repos=[{"name": GAMMA}])

    await tracker.refresh()
    snap = tracker.snapshot()
    assert snap["status"] == "ok"
    assert snap["items"] == []  # the poller will not take it
    assert gh.event_calls == [(GAMMA, 366)]

    clock[0] += 599
    await tracker.refresh()
    assert gh.event_calls == [(GAMMA, 366)]

    clock[0] += 1
    await tracker.refresh()
    assert gh.event_calls == [(GAMMA, 366), (GAMMA, 366)]


@pytest.mark.asyncio
async def test_failed_assignment_check_is_unknown_and_retried(tmp_path: Path) -> None:
    """A failed self-assignment check is a failed GitHub call: the line goes
    unknown and keeps the last good answer, rather than publishing "ok"."""
    beta48 = [i for i in _issues() if i["number"] == 48]
    gamma = [i for i in _issues() if i["repo"] == GAMMA]
    gh = _FakeGitHub(
        [beta48, beta48 + gamma, beta48 + gamma],
        events={(GAMMA, 366): GitHubError("gh failed: HTTP 502")},
    )
    clock = [1_800_000_000]
    tracker = _tracker(
        tmp_path, gh, clock, repos=[_accepting(BETA), {"name": GAMMA}],
    )

    await tracker.refresh()
    good = tracker.snapshot()
    assert good["status"] == "ok"
    assert [i["issue_number"] for i in good["items"]] == [48]

    clock[0] += 60
    await tracker.refresh()
    snap = tracker.snapshot()
    assert snap["status"] == "unknown"
    assert "HTTP 502" in snap["error"]
    assert snap["items"] == good["items"]
    assert snap["last_good_ts"] == 1_800_000_000
    assert snap["checked_ts"] == 1_800_000_060

    gh.events[(GAMMA, 366)] = _assigned_by("octo")
    clock[0] += 60
    await tracker.refresh()
    snap = tracker.snapshot()
    assert snap["status"] == "ok"
    assert [(i["issue_number"], i["reason"]) for i in snap["items"]] == [
        (48, "next poll"), (366, "next poll"),
    ]
    assert gh.event_calls == [(GAMMA, 366), (GAMMA, 366)]


@pytest.mark.asyncio
async def test_only_label_passing_candidates_are_checked(tmp_path: Path) -> None:
    """Excluded, seen and accepting-repo issues cost no events call."""
    state_file = tmp_path / "poller_state.json"
    IssuePoller(
        github=GitHubCLI(), username="octo", repos=[BETA], state_file=state_file,
    ).mark_seen(BETA, 46)
    gh = _FakeGitHub(
        [_issues()], events={(GAMMA, 366): _assigned_by("octo")},
    )
    tracker = _tracker(tmp_path, gh, [1_800_000_000], repos=[
        # BETA#48 and #47 carry the excluded label; #46 does not but is seen.
        {"name": BETA, "automation": {"exclude_labels": ["epic"]}},
        {"name": GAMMA},
        _accepting(ALPHA),
    ], poller_state_file=state_file)
    beta46 = next(i for i in _issues() if i["number"] == 46)
    beta46["labels"] = []
    gh.results = [[i for i in _issues() if i["number"] != 46] + [beta46]]

    await tracker.refresh()

    assert gh.event_calls == [(GAMMA, 366)]
    assert [i["issue_number"] for i in tracker.snapshot()["items"]] == [366, 304]


@pytest.mark.asyncio
async def test_include_label_issues_are_fetched_like_the_poller(
    tmp_path: Path,
) -> None:
    """A repo with include_labels is also asked per label; a label-only
    issue (assigned to someone else) appears after the assigned ones, and
    an issue both assigned and labelled appears once."""
    ri = [i for i in _issues() if i["repo"] == GAMMA]
    labelled_only = _issue_list_row(900, ["ctrlrelay:auto"], assignees=["teammate"])
    also_assigned = _issue_list_row(366, ["ctrlrelay:auto"], assignees=["octo"])
    gh = _FakeGitHub(
        [ri],
        by_label={(GAMMA, "ctrlrelay:auto"): [also_assigned, labelled_only]},
        events={(GAMMA, 366): _assigned_by("octo")},
    )
    tracker = _tracker(tmp_path, gh, [1_800_000_000], repos=[
        {"name": GAMMA, "automation": {"include_labels": ["ctrlrelay:auto"]}},
        _accepting(BETA),
    ])

    await tracker.refresh()
    snap = tracker.snapshot()

    assert gh.label_calls == [(GAMMA, "ctrlrelay:auto")]
    assert snap["status"] == "ok"
    assert [(i["repo"], i["issue_number"], i["reason"]) for i in snap["items"]] == [
        (GAMMA, 366, "next poll"),
        (GAMMA, 900, "next poll"),
    ]
    # Both are admitted by the label (366's label came from the label
    # query), so neither needs an assignment check.
    assert gh.event_calls == []


@pytest.mark.asyncio
async def test_label_query_failure_is_unknown(tmp_path: Path) -> None:
    class _BrokenLabels(_FakeGitHub):
        async def list_issues_by_label(self, repo: str, label: str, **_):
            raise GitHubError("gh failed: label query")

    gh = _BrokenLabels([[]])
    tracker = _tracker(tmp_path, gh, [1_800_000_000], repos=[
        {"name": GAMMA, "automation": {"include_labels": ["ctrlrelay:auto"]}},
    ])

    await tracker.refresh()
    snap = tracker.snapshot()

    assert snap["status"] == "unknown"
    assert "label query" in snap["error"]


@pytest.mark.asyncio
async def test_search_at_its_limit_is_unknown(tmp_path: Path) -> None:
    """A search that returns exactly its limit may have been cut short, and
    a cut-short line would read as complete."""
    one = [i for i in _issues() if i["number"] == 48]
    two = [i for i in _issues() if i["number"] in (48, 47)]
    gh = _FakeGitHub([one, two])
    clock = [1_800_000_000]
    tracker = _tracker(tmp_path, gh, clock, search_limit=2)

    await tracker.refresh()
    assert tracker.snapshot()["status"] == "ok"
    assert gh.limits == [2]

    clock[0] += 60
    await tracker.refresh()
    snap = tracker.snapshot()

    assert snap["status"] == "unknown"
    assert "too many assigned issues to list" in snap["error"]
    assert [i["issue_number"] for i in snap["items"]] == [48]
    assert snap["last_good_ts"] == 1_800_000_000


@pytest.mark.asyncio
async def test_label_added_between_the_two_queries_is_kept(tmp_path: Path) -> None:
    """The search row predates a label the label query already sees; the
    merged issue carries both, so it is admitted by the label."""
    gamma = [i for i in _issues() if i["repo"] == GAMMA]
    assert "ctrlrelay:auto" not in {lab["name"] for lab in gamma[0]["labels"]}
    relabelled = _issue_list_row(366, ["bug", "ctrlrelay:auto"], assignees=["octo"])
    gh = _FakeGitHub(
        [gamma],
        by_label={(GAMMA, "ctrlrelay:auto"): [relabelled]},
        events={(GAMMA, 366): _assigned_by("someone-else")},
    )
    tracker = _tracker(tmp_path, gh, [1_800_000_000], repos=[
        {"name": GAMMA, "automation": {"include_labels": ["ctrlrelay:auto"]}},
    ])

    await tracker.refresh()
    snap = tracker.snapshot()

    assert snap["status"] == "ok"
    assert [(i["issue_number"], i["reason"]) for i in snap["items"]] == [
        (366, "next poll"),
    ]
    assert gh.event_calls == []


@pytest.mark.asyncio
async def test_reassigned_issue_is_checked_again(tmp_path: Path) -> None:
    """A cached "self-assigned" is forgotten once the issue leaves the
    search, so an unassign-then-reassign by someone else is caught."""
    gamma = [i for i in _issues() if i["repo"] == GAMMA]
    gh = _FakeGitHub(
        [gamma, [], gamma], events={(GAMMA, 366): _assigned_by("octo")},
    )
    clock = [1_800_000_000]
    tracker = _tracker(tmp_path, gh, clock, repos=[{"name": GAMMA}])

    await tracker.refresh()
    assert gh.event_calls == [(GAMMA, 366)]

    gh.events[(GAMMA, 366)] = _assigned_by("someone-else")
    clock[0] += 60
    await tracker.refresh()  # unassigned: gone from the search
    assert tracker.snapshot()["items"] == []

    clock[0] += 60
    await tracker.refresh()  # reassigned, by someone else this time
    assert gh.event_calls == [(GAMMA, 366), (GAMMA, 366)]
    assert tracker.snapshot()["items"] == []


@pytest.mark.asyncio
async def test_refresh_never_writes_the_poller_state_file(tmp_path: Path) -> None:
    """Self-assigned, foreign and failed checks all go through the poller's
    own code; none of them may touch the poller's state file."""
    state_file = tmp_path / "poller_state.json"
    IssuePoller(
        github=GitHubCLI(), username="octo", repos=[BETA], state_file=state_file,
    ).mark_seen(BETA, 1)
    before = (state_file.read_bytes(), state_file.stat().st_mtime_ns)

    gamma = [i for i in _issues() if i["repo"] == GAMMA]
    alpha = [i for i in _issues() if i["repo"] == ALPHA]
    gh = _FakeGitHub(
        [gamma + alpha] * 3,
        events={
            (GAMMA, 366): _assigned_by("someone-else"),
            (ALPHA, 304): _assigned_by("octo"),
        },
    )
    clock = [1_800_000_000]
    tracker = _tracker(
        tmp_path, gh, clock, repos=[{"name": GAMMA}, {"name": ALPHA}],
        poller_state_file=state_file,
    )

    await tracker.refresh()
    assert tracker.snapshot()["status"] == "ok"
    clock[0] += 3600
    gh.events[(GAMMA, 366)] = GitHubError("gh failed: HTTP 502")
    await tracker.refresh()
    assert tracker.snapshot()["status"] == "unknown"
    await tracker.refresh()

    assert set(gh.event_calls) == {(GAMMA, 366), (ALPHA, 304)}
    assert (state_file.read_bytes(), state_file.stat().st_mtime_ns) == before


@pytest.mark.asyncio
async def test_changed_issue_is_checked_again(tmp_path: Path) -> None:
    """An unassign and reassign between two refreshes leaves the issue in
    both searches, but bumps its updatedAt, so the cached answer is not
    reused."""
    gamma = [i for i in _issues() if i["repo"] == GAMMA]
    newer = copy.deepcopy(gamma)
    newer[0]["updatedAt"] = "2026-10-27T08:00:00Z"
    gh = _FakeGitHub(
        [gamma, gamma, newer], events={(GAMMA, 366): _assigned_by("octo")},
    )
    clock = [1_800_000_000]
    tracker = _tracker(tmp_path, gh, clock, repos=[{"name": GAMMA}])

    await tracker.refresh()
    await tracker.refresh()  # unchanged: cached
    assert gh.event_calls == [(GAMMA, 366)]

    gh.events[(GAMMA, 366)] = _assigned_by("someone-else")
    await tracker.refresh()

    assert gh.event_calls == [(GAMMA, 366), (GAMMA, 366)]
    assert tracker.snapshot()["items"] == []


@pytest.mark.asyncio
async def test_titles_include_running_issues_and_survive_a_failed_refresh(
    tmp_path: Path,
) -> None:
    """Titles come from the search before any filtering, so an issue that is
    running (and so never in the waiting line) still has its title."""
    issues = _issues()
    running_one = next(i for i in issues if i["repo"] == BETA)
    gh = _FakeGitHub([issues, GitHubError("gh failed: API rate limit exceeded")])
    clock = [1_800_000_000]
    config = _config(tmp_path, [_accepting(BETA)])
    running = [{"session_id": "s1", "repo": BETA,
                "issue_number": running_one["number"], "pipeline": "dev"}]
    tracker = WaitingTracker(
        config, gh,  # type: ignore[arg-type]
        sessions=lambda: (running, {BETA: "s1"}),
        clock=lambda: clock[0],
    )
    assert tracker.titles() == {}

    await tracker.refresh()
    key = f"{BETA}#{running_one['number']}"
    assert key not in {
        f"{i['repo']}#{i['issue_number']}" for i in tracker.snapshot()["items"]
    }
    titles = tracker.titles()
    assert titles[key] == running_one["title"]
    assert titles == {f"{i['repo']}#{i['number']}": i["title"] for i in issues}

    clock[0] += 60
    await tracker.refresh()
    assert tracker.snapshot()["status"] == "unknown"
    assert tracker.titles() == titles
