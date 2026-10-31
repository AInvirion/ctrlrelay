"""Tests for the status page's GitHub links (#232).

Every link the page or the JSON carries is built by ``github_url`` from a
repository name and a number, and is either a github.com issue or pull
request address or ``None``. Nothing a GitHub user or an agent wrote (a
title, a ``pr_url`` an agent reported) can become a link.
"""

from __future__ import annotations

import pytest

from ctrlrelay.web.links import github_url


def test_issue_and_pull_urls_are_built_from_their_parts() -> None:
    assert github_url("example-org/alpha", 5) == (
        "https://github.com/example-org/alpha/issues/5"
    )
    assert github_url("example-org/alpha", 5, kind="pull") == (
        "https://github.com/example-org/alpha/pull/5"
    )
    # Every character the pattern admits, in one name.
    assert github_url("Org_1.x-y/Repo.Name_2-z", 12) == (
        "https://github.com/Org_1.x-y/Repo.Name_2-z/issues/12"
    )


@pytest.mark.parametrize("repo", [
    "alpha",                     # no owner
    "a/b/c",                     # extra segment
    "example-org/alpha\n",       # Python's $ matches before a final newline
    "example-org/alpha/",
    "/alpha",
    "example-org/",
    "../alpha",                  # a segment of dots resolves away in a URL
    "example-org/..",
    "example-org/.",
    "exa mple/alpha",
    "example-org/alpha?x=1",
    "example-org/alpha#1",
    "evil.com@x/alpha",
    "example-org\\alpha",
    "ex%61mple/alpha",
    "",
    None,
    42,
])
def test_a_repo_that_is_not_owner_slash_name_gives_no_link(repo) -> None:
    assert github_url(repo, 5) is None


@pytest.mark.parametrize("number", [0, -1, "5", 5.0, True, False, None, [5]])
def test_a_number_that_is_not_a_positive_int_gives_no_link(number) -> None:
    assert github_url("example-org/alpha", number) is None


def test_an_unknown_kind_is_refused() -> None:
    with pytest.raises(ValueError):
        github_url("example-org/alpha", 5, kind="commit")
