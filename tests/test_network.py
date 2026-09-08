"""Tests for the gh failure classifier (`ctrlrelay.core.network`)."""

from __future__ import annotations

import subprocess

import pytest

from ctrlrelay.core.network import (
    GhFailureKind,
    classify_gh_failure,
    gh_failure_from_exception,
)

# Representative stderr strings. The offline one is gh's own wording,
# copied from a real poll cycle on a disconnected laptop.
NETWORK_STDERR = [
    (
        "error connecting to api.github.com\n"
        "check your internet connection or https://githubstatus.com"
    ),
    'Get "https://api.github.com/user": dial tcp: lookup api.github.com: '
    "no such host",
    'Get "https://api.github.com/user": dial tcp 140.82.121.5:443: '
    "connect: network is unreachable",
    'Get "https://api.github.com/user": dial tcp 140.82.121.5:443: '
    "connect: no route to host",
    'Post "https://api.github.com/graphql": net/http: TLS handshake timeout',
    'Get "https://api.github.com/user": x509: certificate signed by '
    "unknown authority",
    'Get "https://api.github.com/user": context deadline exceeded '
    "(Client.Timeout exceeded while awaiting headers)",
    "read tcp 10.0.0.2:52344->140.82.121.5:443: connection reset by peer",
    "Temporary failure in name resolution",
]

AUTH_STDERR = [
    "gh: Bad credentials (HTTP 401)",
    "gh: Requires authentication (HTTP 401)",
    "To get started with GitHub CLI, please run:  gh auth login",
    "error: not logged into any GitHub hosts. Run gh auth login to authenticate.",
    "gh: This API operation needs the 'read:org' scope. "
    "Please make sure your token has the required scopes.",
]

RATE_LIMIT_STDERR = [
    "gh: API rate limit exceeded for user ID 12345. (HTTP 403)",
    "gh: You have exceeded a secondary rate limit (HTTP 403)",
    "gh: Too Many Requests (HTTP 429)",
]

API_ERROR_STDERR = [
    "gh: Not Found (HTTP 404)",
    "gh: Server Error (HTTP 500)",
    "gh: Validation Failed (HTTP 422)",
    "some unfamiliar failure",
    "",
]


class TestClassification:
    @pytest.mark.parametrize("stderr", NETWORK_STDERR)
    def test_network_failures(self, stderr: str) -> None:
        failure = classify_gh_failure(stderr, returncode=1)
        assert failure.kind is GhFailureKind.NETWORK_UNAVAILABLE

    @pytest.mark.parametrize("stderr", AUTH_STDERR)
    def test_auth_failures(self, stderr: str) -> None:
        failure = classify_gh_failure(stderr, returncode=1)
        assert failure.kind is GhFailureKind.AUTH

    @pytest.mark.parametrize("stderr", RATE_LIMIT_STDERR)
    def test_rate_limit_failures(self, stderr: str) -> None:
        failure = classify_gh_failure(stderr, returncode=1)
        assert failure.kind is GhFailureKind.RATE_LIMITED

    @pytest.mark.parametrize("stderr", API_ERROR_STDERR)
    def test_other_failures_are_api_errors(self, stderr: str) -> None:
        failure = classify_gh_failure(stderr, returncode=1)
        assert failure.kind is GhFailureKind.API_ERROR

    def test_rate_limit_wins_over_auth(self) -> None:
        """A 403 rate-limit body mentions the authenticated user; it must
        still classify as rate limiting, not as an auth problem."""
        stderr = (
            "gh: API rate limit exceeded for user ID 1. "
            "Authenticated requests get a higher rate limit. (HTTP 403)"
        )
        assert (
            classify_gh_failure(stderr, returncode=1).kind
            is GhFailureKind.RATE_LIMITED
        )

    def test_network_wins_over_api_status(self) -> None:
        """A connection error that happens to carry a status-looking string
        is still a network failure."""
        stderr = "error connecting to api.github.com (HTTP 000)"
        assert (
            classify_gh_failure(stderr, returncode=1).kind
            is GhFailureKind.NETWORK_UNAVAILABLE
        )

    def test_classification_is_case_insensitive(self) -> None:
        assert (
            classify_gh_failure("ERROR CONNECTING TO API.GITHUB.COM").kind
            is GhFailureKind.NETWORK_UNAVAILABLE
        )

    def test_bytes_stderr_is_decoded(self) -> None:
        failure = classify_gh_failure(b"gh: Bad credentials (HTTP 401)")
        assert failure.kind is GhFailureKind.AUTH

    def test_none_stderr_is_api_error(self) -> None:
        failure = classify_gh_failure(None, returncode=1)
        assert failure.kind is GhFailureKind.API_ERROR
        assert failure.stderr == ""


class TestFailureDetails:
    def test_http_status_is_extracted(self) -> None:
        failure = classify_gh_failure("gh: Server Error (HTTP 500)")
        assert failure.status == 500

    def test_no_http_status_is_none(self) -> None:
        failure = classify_gh_failure("error connecting to api.github.com")
        assert failure.status is None

    def test_returncode_is_carried(self) -> None:
        assert classify_gh_failure("boom", returncode=2).returncode == 2

    def test_stderr_is_stripped_and_kept(self) -> None:
        failure = classify_gh_failure("  gh: Not Found (HTTP 404)\n")
        assert failure.stderr == "gh: Not Found (HTTP 404)"


class TestMessages:
    def test_network_message_says_offline_not_auth(self) -> None:
        message = classify_gh_failure(
            "error connecting to api.github.com"
        ).message
        assert "network" in message.lower()
        assert "auth" not in message.lower()

    def test_auth_message_suggests_gh_auth_status(self) -> None:
        message = classify_gh_failure("gh: Bad credentials (HTTP 401)").message
        assert "gh auth status" in message

    def test_rate_limit_message_mentions_rate_limit(self) -> None:
        message = classify_gh_failure(
            "gh: API rate limit exceeded (HTTP 403)"
        ).message
        assert "rate limit" in message.lower()

    def test_api_error_message_surfaces_status(self) -> None:
        message = classify_gh_failure("gh: Server Error (HTTP 500)").message
        assert "500" in message

    def test_api_error_message_without_status_still_readable(self) -> None:
        message = classify_gh_failure("weird thing happened").message
        assert message
        assert "GitHub API" in message


class TestFromException:
    def test_called_process_error_with_text_stderr(self) -> None:
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "api", "user"],
            stderr="error connecting to api.github.com",
        )
        failure = gh_failure_from_exception(exc)
        assert failure.kind is GhFailureKind.NETWORK_UNAVAILABLE
        assert failure.returncode == 1

    def test_called_process_error_with_bytes_stderr(self) -> None:
        exc = subprocess.CalledProcessError(
            returncode=1,
            cmd=["gh", "api", "user"],
            stderr=b"gh: Bad credentials (HTTP 401)",
        )
        assert gh_failure_from_exception(exc).kind is GhFailureKind.AUTH

    def test_called_process_error_without_stderr(self) -> None:
        exc = subprocess.CalledProcessError(
            returncode=1, cmd=["gh", "api", "user"]
        )
        failure = gh_failure_from_exception(exc)
        assert failure.kind is GhFailureKind.API_ERROR
        assert failure.returncode == 1

    def test_os_error_points_at_the_gh_install_not_the_network(self) -> None:
        """`subprocess.run` raising OSError means the child could not be
        EXEC'd. Python does no networking here — gh does — so even an
        errno that reads like connectivity (ENETUNREACH) reached us
        because the spawn failed, not because a socket did. Calling it
        "offline" sends the operator to check their wifi while their gh
        install is broken."""
        failure = gh_failure_from_exception(
            OSError(101, "Network is unreachable")
        )
        assert failure.kind is GhFailureKind.GH_MISSING

    def test_timeout_expired_is_network_unavailable(self) -> None:
        exc = subprocess.TimeoutExpired(cmd=["gh", "api", "user"], timeout=30)
        assert (
            gh_failure_from_exception(exc).kind
            is GhFailureKind.NETWORK_UNAVAILABLE
        )

    def test_file_not_found_is_not_swallowed_as_network(self) -> None:
        """gh missing from PATH is a setup problem, not connectivity."""
        failure = gh_failure_from_exception(
            FileNotFoundError(2, "No such file or directory")
        )
        assert failure.kind is GhFailureKind.GH_MISSING
        assert "gh" in failure.message


class TestExecFailuresAreNotReportedAsOffline:
    """`subprocess.run` raising OSError means the child could not be
    EXEC'd — Python is not doing the networking here, gh is. Classifying
    those as NETWORK_UNAVAILABLE is the exact misdiagnosis this module
    exists to prevent: it sends the operator to check their wifi while
    their gh install is broken."""

    @pytest.mark.parametrize(
        "exc",
        [
            PermissionError(13, "Permission denied"),
            IsADirectoryError(21, "Is a directory"),
            NotADirectoryError(20, "Not a directory"),
            OSError(8, "Exec format error"),
        ],
        ids=["not-executable", "is-a-directory", "bad-path", "wrong-arch"],
    )
    def test_exec_failure_points_at_the_install(self, exc: OSError) -> None:
        from ctrlrelay.core.network import GhFailureKind, gh_failure_from_exception

        failure = gh_failure_from_exception(exc)

        assert failure.kind is GhFailureKind.GH_MISSING
        assert "network" not in failure.message.lower()
        assert "connectivity" not in failure.message.lower()

    def test_a_hung_gh_still_reads_as_a_network_problem(self) -> None:
        """The one exception shape that really does suggest connectivity:
        the child launched and then stopped responding."""
        from ctrlrelay.core.network import GhFailureKind, gh_failure_from_exception

        assert (
            gh_failure_from_exception(TimeoutError("timed out")).kind
            is GhFailureKind.NETWORK_UNAVAILABLE
        )

    def test_missing_binary_message_covers_unusable_not_just_absent(self) -> None:
        """A gh that exists but cannot run is not 'not found'."""
        from ctrlrelay.core.network import gh_failure_from_exception

        message = gh_failure_from_exception(
            PermissionError(13, "Permission denied")
        ).message

        assert "could not be run" in message
        assert "executable" in message
