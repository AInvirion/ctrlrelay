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


def fake_relay_publish(
    github, *, pr_number: int = 42, repo: str = "owner/repo",
    head_sha: str = "c0ffee",
):
    github.get_default_branch = AsyncMock(return_value="main")
    # The review base's head, as GitHub names it (#263).
    github.get_ref_sha = AsyncMock(return_value="b" * 40)
    github.list_prs = AsyncMock(return_value=[])
    github.create_pr = AsyncMock(
        return_value={
            "number": pr_number,
            "url": f"https://github.com/{repo}/pull/{pr_number}",
        }
    )
    # The PR's head as GitHub reports it after the review (#222): the
    # commit the dev tests' fake push returns, so the review is current.
    github.get_pr_head = AsyncMock(
        return_value={"sha": head_sha, "draft": False}
    )
    github.set_pr_draft = AsyncMock(return_value=None)
    return github
