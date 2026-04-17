"""GitHub Issue Poller for dev-sync."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dev_sync.core.github import GitHubCLI


@dataclass
class IssuePoller:
    """Polls GitHub repos for newly assigned issues.

    Maintains a set of seen issue numbers per repo so that only genuinely new
    issues are surfaced on each call to ``poll()``.
    """

    github: GitHubCLI
    username: str
    repos: list[str]
    state_file: Path
    seen_issues: dict[str, set[int]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        """Load seen issues from the JSON state file (if it exists)."""
        if not self.state_file.exists():
            return
        try:
            data = json.loads(self.state_file.read_text())
            raw = data.get("seen_issues", {})
            self.seen_issues = {repo: set(numbers) for repo, numbers in raw.items()}
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable state — start fresh
            self.seen_issues = {}

    def _save_state(self) -> None:
        """Persist seen issues and a ``last_poll`` timestamp to the state file."""
        data = {
            "seen_issues": {
                repo: sorted(numbers) for repo, numbers in self.seen_issues.items()
            },
            "last_poll": datetime.now(timezone.utc).isoformat(),
        }
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(data, indent=2))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def poll(self) -> list[dict[str, Any]]:
        """Poll all configured repos for new issues assigned to ``self.username``.

        Returns:
            A list of ``{"repo": str, "issue": dict}`` entries for issues that
            have not been seen before.  Updates ``seen_issues`` and persists
            state to disk.
        """
        new_issues: list[dict[str, Any]] = []

        for repo in self.repos:
            issues = await self.github.list_assigned_issues(
                repo, assignee=self.username
            )
            seen_for_repo = self.seen_issues.setdefault(repo, set())

            for issue in issues:
                number: int = issue["number"]
                if number not in seen_for_repo:
                    new_issues.append({"repo": repo, "issue": issue})
                    seen_for_repo.add(number)

        self._save_state()
        return new_issues

    def mark_seen(self, repo: str, issue_number: int) -> None:
        """Mark an issue as seen without triggering a poll.

        Useful for pre-seeding state from external sources (e.g. resuming
        after a crash where work was already started).
        """
        self.seen_issues.setdefault(repo, set()).add(issue_number)
