"""Fake GitHub side of the relay's publish step (#219).

Since #219 the dev agent only commits; the relay pushes the branch and
opens the pull request. Tests that drive ``run_dev_issue`` through to a
DONE need the three ``gh`` calls that step makes to answer like GitHub
would. A bare ``AsyncMock`` answers ``MagicMock`` objects, which
``int()`` turns into 1, so a pull request number would silently read
as #1.
"""

from __future__ import annotations

from unittest.mock import AsyncMock


def fake_relay_publish(github, *, pr_number: int = 42, repo: str = "owner/repo"):
    github.get_default_branch = AsyncMock(return_value="main")
    github.list_prs = AsyncMock(return_value=[])
    github.create_pr = AsyncMock(
        return_value={
            "number": pr_number,
            "url": f"https://github.com/{repo}/pull/{pr_number}",
        }
    )
    return github
