"""Classify `gh` subprocess failures so operators get an actionable message.

`gh` exits 1 for everything: an unplugged network cable, an expired token
and a 500 from the API all surface as
``returned non-zero exit status 1``. That sends operators debugging auth
when the real problem is that the laptop is offline (#32).

`gh` does write clear wording to stderr, so classification keys off the
stderr text rather than the exit code.
"""

from __future__ import annotations

import errno
import re
import subprocess
from dataclasses import dataclass
from enum import Enum


class GhFailureKind(str, Enum):
    """What actually went wrong behind a non-zero `gh` exit."""

    NETWORK_UNAVAILABLE = "network_unavailable"
    AUTH = "auth"
    RATE_LIMITED = "rate_limited"
    API_ERROR = "api_error"
    GH_MISSING = "gh_missing"
    TLS_TRUST = "tls_trust"


# Ordered most-specific-first within each group; matched case-insensitively
# against gh's stderr. Network patterns cover gh's own wording plus the Go
# net/http and OpenSSL strings it passes through verbatim.
_NETWORK_PATTERNS = (
    "error connecting to",
    "dial tcp",
    "no such host",
    "network is unreachable",
    "no route to host",
    "connection refused",
    "connection timed out",
    "connection reset by peer",
    # A peer that vanished mid-write. Deliberately not matching bare
    # "EOF" alongside it: gh passes API response bodies through verbatim
    # and "EOF" is common enough in them to misclassify real API errors
    # as outages, which is the mistake in the other direction.
    "broken pipe",
    "i/o timeout",
    "tls handshake",
    "x509:",
    "certificate signed by",
    "context deadline exceeded",
    "temporary failure in name resolution",
    "name resolution",
    "proxyconnect tcp",
    "server misbehaving",
    "check your internet connection",
)

# errnos `subprocess.run` raises when it cannot EXEC the child — the file
# is absent, unreadable, a directory, or the wrong architecture. These
# say "your gh install is broken". Resource-exhaustion errnos (EMFILE,
# ENOMEM, EAGAIN) also surface as OSError here but mean nothing of the
# sort, and telling that operator to install GitHub CLI sends them the
# wrong way.
_EXEC_FAILURE_ERRNOS = frozenset(
    e for e in (
        errno.ENOENT, errno.EACCES, errno.EPERM, errno.ENOEXEC,
        errno.EISDIR, errno.ENOTDIR, errno.ELOOP,
        errno.ENAMETOOLONG, errno.ETXTBSY,
    )
)

# Persistent local trust problems, not outages: a corporate MITM proxy
# without its CA installed, or a clock skewed far enough to invalidate a
# valid certificate. "Retry later" is wrong advice for both. A cert valid
# for the wrong host IS usually a captive portal, so that stays network.
_TLS_TRUST_PATTERNS = (
    "certificate signed by unknown authority",
    "certificate has expired or is not yet valid",
    "unable to get local issuer certificate",
    "self-signed certificate",
    "self signed certificate",
)

_RATE_LIMIT_PATTERNS = (
    "rate limit exceeded",
    "secondary rate limit",
    "too many requests",
    "http 429",
)

_AUTH_PATTERNS = (
    "bad credentials",
    "requires authentication",
    "gh auth login",
    "not logged in",
    "authentication failed",
    "http 401",
    "required scopes",
    "needs the '",
    "token has not been granted",
    "gh_token",
    "github_token",
)

_HTTP_STATUS_RE = re.compile(r"\(?\bHTTP (\d{3})\b\)?", re.IGNORECASE)


@dataclass(frozen=True)
class GhFailure:
    """A classified `gh` failure plus the raw evidence behind it."""

    kind: GhFailureKind
    message: str
    stderr: str = ""
    returncode: int | None = None
    status: int | None = None

    @property
    def is_transient(self) -> bool:
        """True when retrying later could plausibly succeed without the
        operator changing anything."""
        return self.kind in (
            GhFailureKind.NETWORK_UNAVAILABLE,
            GhFailureKind.RATE_LIMITED,
        )


def _decode(stderr: str | bytes | None) -> str:
    if stderr is None:
        return ""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("utf-8", errors="replace")
    return stderr.strip()


# Go renders a failed HTTP round-trip as `*url.Error`:
#   Get "https://api.github.com/user": <cause>
# By construction that means NO HTTP response was received, so it can
# never be an API error — whatever the cause reads like. `gh api` prints
# a real API error in the other shape entirely, `gh: <msg> (HTTP nnn)`,
# so the two do not overlap. Keying on the shape catches the whole family
# (EOF, http2 connection lost, request canceled, remote error: tls: …)
# instead of chasing each new wording as a string.
_URL_ERROR_RE = re.compile(r'^(?:get|post|put|patch|delete|head)\s+"https?://', re.I)


def _is_transport_failure(text: str) -> bool:
    """True when the text is Go's url.Error shape — request issued, no
    response received."""
    return any(
        _URL_ERROR_RE.match(line.strip())
        for line in text.splitlines()
        if line.strip()
    )


def _kind_for(text: str) -> GhFailureKind:
    lowered = text.lower()
    # Checked before the network patterns: these strings contain
    # "x509:", but retrying never fixes a CA bundle or a skewed clock.
    if any(pattern in lowered for pattern in _TLS_TRUST_PATTERNS):
        return GhFailureKind.TLS_TRUST
    if any(pattern in lowered for pattern in _NETWORK_PATTERNS):
        return GhFailureKind.NETWORK_UNAVAILABLE
    if _is_transport_failure(text):
        return GhFailureKind.NETWORK_UNAVAILABLE
    # Rate-limit bodies mention authentication ("Authenticated requests get
    # a higher rate limit"), so they have to be matched before auth.
    if any(pattern in lowered for pattern in _RATE_LIMIT_PATTERNS):
        return GhFailureKind.RATE_LIMITED
    if any(pattern in lowered for pattern in _AUTH_PATTERNS):
        return GhFailureKind.AUTH
    return GhFailureKind.API_ERROR


def _message_for(kind: GhFailureKind, status: int | None) -> str:
    if kind is GhFailureKind.NETWORK_UNAVAILABLE:
        return (
            "Network unavailable — could not reach api.github.com. "
            "Check connectivity or https://githubstatus.com, then retry."
        )
    if kind is GhFailureKind.RATE_LIMITED:
        return (
            "GitHub rate limit exceeded — wait for the limit to reset, "
            "then retry."
        )
    if kind is GhFailureKind.AUTH:
        return (
            "GitHub credentials rejected — run `gh auth status` "
            "(then `gh auth login`) and retry."
        )
    if kind is GhFailureKind.TLS_TRUST:
        return (
            "TLS certificate not trusted — the connection to api.github.com "
            "was intercepted or the certificate could not be verified. "
            "Install your proxy's CA certificate, or check the system clock. "
            "Retrying will not help."
        )
    if kind is GhFailureKind.GH_MISSING:
        return (
            "The `gh` CLI could not be run — install GitHub CLI, make sure "
            "it is on PATH, and check the file is executable."
        )
    if status is not None:
        return f"GitHub API returned an error (HTTP {status})."
    return "GitHub API call failed."


def classify_gh_failure(
    stderr: str | bytes | None, returncode: int | None = None
) -> GhFailure:
    """Classify one `gh` failure from the stderr it wrote."""
    text = _decode(stderr)
    kind = _kind_for(text)
    match = _HTTP_STATUS_RE.search(text)
    status = int(match.group(1)) if match else None
    return GhFailure(
        kind=kind,
        message=_message_for(kind, status),
        stderr=text,
        returncode=returncode,
        status=status,
    )


def gh_failure_from_exception(exc: BaseException) -> GhFailure:
    """Classify whatever a `gh` subprocess call raised.

    Handles the three things `subprocess.run(..., check=True)` can throw at
    a caller: a non-zero exit, a missing binary, and a timeout — plus bare
    `OSError` for socket-level failures raised by Python rather than gh.
    """
    if isinstance(exc, FileNotFoundError):
        # Checked before OSError: a missing gh is a setup problem, and
        # calling it "offline" would send operators the wrong way.
        return GhFailure(
            kind=GhFailureKind.GH_MISSING,
            message=_message_for(GhFailureKind.GH_MISSING, None),
            stderr=str(exc),
        )
    if isinstance(exc, subprocess.CalledProcessError):
        return classify_gh_failure(exc.stderr, returncode=exc.returncode)
    if isinstance(exc, (subprocess.TimeoutExpired, TimeoutError)):
        # A hung gh is the one exception shape that really does suggest
        # connectivity: the child launched and then stopped responding.
        return GhFailure(
            kind=GhFailureKind.NETWORK_UNAVAILABLE,
            message=_message_for(GhFailureKind.NETWORK_UNAVAILABLE, None),
            stderr=str(exc),
        )
    if isinstance(exc, OSError):
        # An OSError here came from `subprocess.run` failing to launch the
        # child. Python is not doing the networking — gh is — so it can
        # never mean "offline"; reporting it that way is the exact
        # misdiagnosis this module exists to prevent.
        #
        # But only the EXEC-shaped errnos mean the install is broken. A
        # process-table or memory limit (EMFILE, ENOMEM, EAGAIN) also
        # lands here, and answering that with "install GitHub CLI" is
        # just the same misdirection wearing a different hat. Fall
        # through for those: the caller still prints the raw errno text,
        # which is the actionable part.
        if exc.errno in _EXEC_FAILURE_ERRNOS:
            return GhFailure(
                kind=GhFailureKind.GH_MISSING,
                message=_message_for(GhFailureKind.GH_MISSING, None),
                stderr=str(exc),
            )
        return GhFailure(
            kind=GhFailureKind.API_ERROR,
            message=f"Could not run `gh`: {exc}",
            stderr=str(exc),
        )
    return classify_gh_failure(str(exc))
