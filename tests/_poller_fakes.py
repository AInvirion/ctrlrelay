"""Fakes for the poller's account-wide search (#282).

The poller asks GitHub once per poll (``search_open_issues``) instead of
once per repo. Most poller tests were written against the per-repo
``list_assigned_issues`` / ``list_issues_by_label`` fakes; ``wire_search``
answers the search from those, repo by repo, so each test keeps saying what
it means. As the real search does, any failure fails the whole search.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock


def wire_search(gh: MagicMock, *, operator: str = "alice") -> MagicMock:
    searched_labels: set[str] = set()

    async def search(*, repos, assignee=None, label=None, limit=1000):
        if label is not None:
            searched_labels.add(label)
        out = {}
        for repo in repos:
            if assignee is not None:
                out[repo] = list(await gh.list_assigned_issues(repo, assignee=assignee))
            else:
                out[repo] = list(await gh.list_issues_by_label(repo, label=label))
        return out

    async def label_events(repo, issue_number):
        # By default the operator added every label, so the label
        # trigger behaves as before #282; tests of that check override it.
        names = sorted(searched_labels)
        return [{"event": "labeled", "actor": {"login": operator},
                 "label": {"name": n}} for n in names]

    gh.search_open_issues = AsyncMock(side_effect=search)
    gh.list_label_events = AsyncMock(side_effect=label_events)
    return gh
