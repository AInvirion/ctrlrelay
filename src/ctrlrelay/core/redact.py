"""Redaction and truncation for worker output that gets persisted.

#204: the agent's exit code and its output were both discarded, so the
only durable evidence of why 163 consecutive dispatches failed was their
duration — about two seconds each. That is enough to *eliminate* causes
and not enough to read one, and the difference decided how long the
outage ran.

Retaining the output is therefore the fix, and retaining it raw is not
an option. A worker's final bytes can carry a credential, a prompt, an
issue body or a customer string, and #160 already paid for writing
operator text to a log in plaintext. So the output is scrubbed and
capped here, and the result goes to the state database — never to the
log stream, which keeps a hash and a length.

Two ordering decisions carry the correctness:

- **Redact before truncating.** Truncating first can cut a token in
  half, and half a token no longer matches the pattern that would have
  caught it — so the fragment is stored verbatim. A prefix of a secret
  is still a secret.
- **Keep the TAIL, not the head.** The reason a process died is in its
  last lines. A head-biased cap stores the banner.
"""

from __future__ import annotations

import re

# How much of the worker's output to keep, per stream. The failure that
# produced this module emitted about 1,252 bytes in total; 2,000 per
# stream keeps that whole and still bounds a worker that decides to
# print a megabyte.
DEFAULT_TAIL_CHARS = 2000

REDACTION = "[redacted]"

# Ordered most-specific first. Each pattern replaces only the secret
# itself where that is separable, so the surrounding text still reads —
# `Authorization: Bearer [redacted]` tells you it was an auth failure,
# which is the whole point of keeping the output at all.
#
# These are deliberately NOT a generic "long random-looking string"
# rule. Such a rule eats commit shas, session ids, stack addresses and
# base64 diffs, which is most of what makes a failure readable; it
# would trade this ticket's defect for a redacted blob that diagnoses
# nothing. Known credential shapes are what gets matched.
_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # JSON Web Tokens — three dot-separated base64url segments starting
    # with the `{"` header prefix. Before the generic key/value rule so
    # a JWT in a header value is matched as a whole.
    (
        re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        REDACTION,
    ),
    # GitHub: ghp_ (classic PAT), gho_, ghu_, ghs_, ghr_, and the
    # github_pat_ fine-grained prefix.
    #
    # NO leading `\b` on any of these, and no trailing one either. A
    # word boundary needs a NON-word character before the prefix, so
    # `...somethingghp_AAA...` — a token concatenated to adjacent output
    # with no separator — does not match, and the credential is stored
    # verbatim. Found by the straddling-token test in
    # `tests/test_redact.py`, which glues the token to its neighbour on
    # purpose.
    #
    # The trade is deliberate and one-directional: without the
    # boundary a token-shaped substring inside an unrelated word is
    # redacted too. That costs one `[redacted]` in diagnostic text. The
    # boundary version costs a leaked credential, and only one of those
    # is recoverable.
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"), REDACTION),
    (re.compile(r"github_pat_[A-Za-z0-9_]{16,}"), REDACTION),
    # Anthropic, OpenAI and the generic `sk-` family.
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"), REDACTION),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), REDACTION),
    # Slack.
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{8,}"), REDACTION),
    # AWS access key id. An AKIA key is exactly 20 characters, but the
    # trailing boundary is dropped for the reason above — against a
    # longer run of capitals this redacts the first 20 and leaves the
    # remainder, which is the safe direction.
    (re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), REDACTION),
    # Private key blocks: everything between the markers goes, because
    # a PEM body has no useful diagnostic content.
    (
        re.compile(
            r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
            re.DOTALL,
        ),
        "-----BEGIN PRIVATE KEY----- [redacted] -----END PRIVATE KEY-----",
    ),
    # `Authorization: Bearer <...>` and `Basic <...>`. The scheme is
    # kept; the credential is not.
    (
        re.compile(r"\b(Bearer|Basic|Token)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
        r"\1 " + REDACTION,
    ),
    # Generic `key = value` shapes. The KEY is kept so the reader can
    # see which setting was wrong. Stops at whitespace, a quote, a
    # comma or a closing brace so one assignment on a crowded line
    # cannot swallow the rest of it.
    #
    # The key is matched with a `[A-Za-z0-9_]*` PREFIX rather than a
    # leading `\b`, and this is the same fault as the token prefixes
    # above — fixed there and left here. A word boundary needs a
    # non-word character before the name, and an underscore is a word
    # character, so `\bsecret\b` does not match inside
    # `AWS_SECRET_ACCESS_KEY`. Measured on the first version of this
    # file: `AWS_SECRET_ACCESS_KEY=`, `GITHUB_TOKEN=`, `MY_API_KEY=`
    # and `X_PASSWORD=` ALL stored their values verbatim. Namespaced
    # environment variables are the overwhelmingly common shape in a
    # worker's output, so the boundary version missed the normal case
    # and matched only the bare one.
    #
    # The trailing boundary goes for the mirror reason: `TOKEN_FOR_GH=`
    # has no boundary after `TOKEN`. What bounds the match instead is
    # the assignment operator that must follow, which is what makes
    # this pattern a key/value rule rather than a word list.
    # `(?![A-Za-z0-9_]*(?:_count|_len|...))` exempts an unambiguous
    # METRIC suffix, so `token_count=8192` and `credential_cache_size=
    # 4096` survive. Losing those was a real cost: this module exists
    # so an operator can tell a credential failure from a crash, and a
    # token count is exactly the kind of number that explains one.
    #
    # The exemption list is of METRIC names, not of credential names.
    # That direction matters: the credential side must stay open-ended
    # because the next secret will have a name nobody listed, while a
    # key literally ending in `_count` or `_bytes` holding a secret
    # would be perverse. An enumeration is safe only where being
    # incomplete costs a redaction rather than a leak.
    (
        re.compile(
            r"(?i)([A-Za-z0-9_]*(?:api[_-]?key|access[_-]?key|access[_-]?token"
            r"|refresh[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret"
            r"|password|passwd|secret|token|credential)"
            r"(?![A-Za-z0-9_]*(?:_count|_len|_length|_size|_bytes|_ms"
            r"|_seconds|_limit|_total)\b)"
            r"[A-Za-z0-9_]*)"
            r"(\s*[:=]\s*|\s+is\s+)"
            r"(\"[^\"\n]{4,}\"|'[^'\n]{4,}'|[^\s,;}\"'\n]{4,})"
        ),
        r"\1\2" + REDACTION,
    ),
)


def redact_secrets(text: str) -> str:
    """Replace known credential shapes in ``text`` with ``[redacted]``.

    Not a proof that nothing sensitive remains — no pattern list is
    that. It removes the shapes a worker's output actually carries
    (tokens the agent echoed, an auth header in a failing `gh` call, a
    key in a traceback's locals) and leaves everything else readable.
    An empty string in gives an empty string out.
    """
    if not text:
        return ""
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def tail(text: str, limit: int = DEFAULT_TAIL_CHARS) -> str:
    """The last ``limit`` characters of ``text``, marked when clipped.

    ``limit <= 0`` returns an empty string rather than the whole input:
    a caller asking for no characters must not be handed everything,
    which is the direction that leaks.
    """
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return "[...truncated]" + text[-limit:]


def worker_output_tail(
    stdout: str | None,
    stderr: str | None,
    *,
    limit: int = DEFAULT_TAIL_CHARS,
) -> str:
    """Scrubbed, capped, labelled tail of a worker's final output.

    Both streams are kept and labelled, because which one carried the
    message is itself diagnostic: a credential failure writes to
    stderr, while an agent that ran and refused writes its reason to
    stdout. Collapsing them would re-lose the distinction the field
    exists to record.

    Redaction runs on each stream BEFORE the cap — see the module
    docstring; truncating first can store half a token.

    Returns ``""`` when both streams are empty, which is a real and
    differently-shaped failure (the agent died before writing
    anything) and must not be dressed up as output.
    """
    parts: list[str] = []
    for label, raw in (("stderr", stderr), ("stdout", stdout)):
        text = (raw or "").strip()
        if not text:
            continue
        clipped = tail(redact_secrets(text), limit)
        parts.append(f"--- {label} ---\n{clipped}")
    return "\n\n".join(parts)
