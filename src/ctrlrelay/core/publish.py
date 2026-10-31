"""The GitHub writes the relay does on the agent's behalf (#219).

Before #219 the dev agent pushed its own branch and opened its own pull
request, and the task agent posted its own report, all with the
operator's login. Now the agent only edits and commits in its worktree,
and the relay does the GitHub side after the agent exits, with its own
credentials. This module holds the two rules that make that safe:

- :func:`check_push_target` decides whether a branch may be pushed at
  all. It is called by the push code itself, not by a prompt, so it
  holds however the agent behaves.
- :func:`sanitize_untrusted_text` and friends shape text the agent
  wrote before the relay posts it under the operator's name.
"""

from __future__ import annotations

import re

from ctrlrelay.core.obs import get_logger

_logger = get_logger("core.publish")

# Never pushed to by the relay, whatever the repository's default is
# and whatever a branch template says.
ALWAYS_PROTECTED_BRANCHES: frozenset[str] = frozenset({"main", "master"})


class PushRefusedError(Exception):
    """A push the relay will not make. The message names why."""


def check_push_target(
    target: str,
    *,
    run_branch: str,
    default_branch: str,
    repo: str | None = None,
    session_id: str | None = None,
) -> None:
    """Raise :class:`PushRefusedError` unless ``target`` may be pushed.

    Allowed only when ``target`` is exactly this run's own branch AND
    that branch is not ``main``, ``master`` or the repository's default
    branch. ``default_branch`` must come from somewhere the agent cannot
    write (the GitHub API), and an empty value is refused rather than
    read as "no default to protect".

    Every refusal is logged at ERROR as ``publish.push.refused``.
    """
    reason: str | None = None
    if not default_branch:
        reason = "default branch unknown; refusing to push blind"
    elif not target or not run_branch:
        reason = "empty branch name"
    elif target in ALWAYS_PROTECTED_BRANCHES:
        reason = f"{target!r} is a protected branch"
    elif target == default_branch:
        reason = f"{target!r} is the repository's default branch"
    elif target != run_branch:
        reason = (
            f"{target!r} is not this run's branch ({run_branch!r})"
        )
    elif target.startswith("refs/") or ":" in target or target.startswith("-"):
        reason = f"{target!r} is not a plain branch name"
    if reason is None:
        return
    _logger.error(
        "publish.push.refused",
        extra={
            "repo": repo,
            "session_id": session_id,
            "branch": target,
            "run_branch": run_branch,
            "default_branch": default_branch,
            "reason": reason,
        },
    )
    raise PushRefusedError(f"push refused: {reason}")


_REPO_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9_.-]+$")


def github_push_url(repo: str) -> str:
    """The explicit URL the relay pushes to, from a validated name.

    Never ``origin``: the remote's URL lives in config the agent can
    write. Raises :class:`PushRefusedError` for anything that is not a
    plain ``owner/name``.
    """
    name = repo.removesuffix(".git")
    if not _REPO_NAME.match(name) or ".." in name:
        raise PushRefusedError(f"push refused: {repo!r} is not owner/name")
    return f"https://github.com/{name}.git"


# --- untrusted text -------------------------------------------------------

PR_TITLE_MAX_CHARS = 200
PR_BODY_MAX_CHARS = 20_000
COMMENT_MAX_CHARS = 20_000

_ZWSP = "​"
# @name and @org/team. A zero-width space after the @ keeps the text
# readable and stops GitHub from notifying anyone.
_MENTION = re.compile(r"@(?=[A-Za-z0-9])")
# GitHub closes the referenced issue on merge when one of these words
# precedes a reference. The agent's text must not be able to close an
# issue the relay did not choose, so a zero-width space is glued to
# the keyword, which GitHub then no longer recognises.
_CLOSING_KEYWORD = re.compile(
    r"\b(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"
    r"(?=\s*:?\s+(?:[\w.-]+/[\w.-]+)?#\d+|\s*:?\s+https?://)",
    re.IGNORECASE,
)
# C0 controls other than tab and newline, plus DEL.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_untrusted_text(text: str, *, max_chars: int) -> str:
    """Make agent-written text safe to post under the operator's name.

    Strips control characters, disables @-mentions and issue-closing
    keywords, and caps the length, saying how much was dropped.
    """
    text = _CONTROL.sub("", text.replace("\r\n", "\n"))
    text = _MENTION.sub("@" + _ZWSP, text)
    text = _CLOSING_KEYWORD.sub(lambda m: m.group(1) + _ZWSP, text)
    if len(text) > max_chars:
        dropped = len(text) - max_chars
        text = (
            text[:max_chars]
            + f"\n\n[truncated by ctrlrelay: {dropped} characters dropped]"
        )
    return text


def build_pr_title(
    agent_title: object, *, issue_number: int, issue_title: str
) -> str:
    """One line, capped. Falls back to the issue's own title."""
    raw = agent_title if isinstance(agent_title, str) else ""
    line = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    line = sanitize_untrusted_text(line, max_chars=10_000)
    if not line:
        line = f"{issue_title.strip()} (#{issue_number})".strip()
    if len(line) > PR_TITLE_MAX_CHARS:
        line = line[: PR_TITLE_MAX_CHARS - 3].rstrip() + "..."
    return line


def build_pr_body(
    agent_body: object, *, issue_number: int, session_id: str
) -> str:
    """Agent text (sanitized, capped) plus the relay's own footer.

    The footer carries the one closing reference the relay chose, so
    the pull request still closes its own issue on merge.
    """
    raw = agent_body if isinstance(agent_body, str) else ""
    body = sanitize_untrusted_text(raw.strip(), max_chars=PR_BODY_MAX_CHARS)
    if not body:
        body = "_The agent did not write a description._"
    footer = (
        f"\n\n---\nCloses #{issue_number}\n\n"
        f"Pushed and opened by ctrlrelay (session `{session_id}`). The "
        "description above was written by the coding agent and is "
        "shown with mentions disabled and its length capped."
    )
    return body + footer
