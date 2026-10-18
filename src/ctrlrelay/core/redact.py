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
# Keys whose value is plausibly a NUMBER and still a secret: a PIN, a
# one-time code, a numeric password. Everywhere else a numeric value is
# a measurement, which is the rule below.
#
# This list is on the dangerous side of the decision deliberately. Being
# incomplete here costs a leak only for a numeric secret under an
# unusual key name; being incomplete on the other side would destroy
# every diagnostic number in the output, which is most of what makes a
# failure readable.
_NUMERIC_SECRET_KEYS = re.compile(
    r"(?i)(?:password|passwd|passphrase|passcode|pin|otp)[A-Za-z0-9_]*$"
)

# Digits, with an optional sign, decimal point or exponent: what a
# measurement looks like. Anchored, so `8192abc` is not a number.
_NUMERIC_VALUE = re.compile(r"^[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _redact_unless_measurement(match: "re.Match[str]") -> str:
    """Keep `max_tokens: 100000`; redact `client_secret="abc"`.

    **The discriminator is the VALUE, not the key.** This rule has been
    patched four times and each patch was a longer name list, so the
    list is gone:

      1. `\bsecret\b` missed `AWS_SECRET_ACCESS_KEY` — an underscore
         is a word character. Boundaries dropped.
      2. The widened rule then ate `token_count=8192`. A metric-suffix
         exemption was added.
      3. That exemption leaked `API_KEY_COUNT=hunter2`, because the
         suffix was the whole test. The value had to be numeric too.
      4. And the suffix list still did not contain `max_tokens`,
         `input_tokens` or `total_tokens` — the exact numbers in the
         API error the alert tells the operator to go and read.

    The fourth correction is the argument for deleting the mechanism.
    A credential in these shapes — a key, a token, a PAT, a client
    secret — is never a bare number, so **a numeric value is a
    measurement** and needs no name list at all. The only exception is
    a key whose secret genuinely is numeric, and that is a short list
    on the side where being wrong costs one redaction.
    """
    name, quote, sep, value = (
        match.group(1),
        match.group(2),
        match.group(3),
        match.group(4),
    )
    bare = value.strip("\"'")
    if _NUMERIC_VALUE.match(bare) and not _NUMERIC_SECRET_KEYS.search(name):
        return match.group(0)
    return f"{name}{quote}{sep}{REDACTION}"


_PATTERNS: tuple[tuple[re.Pattern[str], str | object], ...] = (
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
    # NO leading boundary or lookbehind on the DISTINCTIVE prefixes. A
    # token can land flush against surrounding output with no
    # separator, and `\b` needs a non-word character before it, so the
    # first version of this file stored `...contextghp_AAA...` intact.
    # `ghp_`, `github_pat_`, `xox?-` and `AKIA` plus their length are
    # specific enough that a mid-word match is a real credential.
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"), REDACTION),
    (re.compile(r"github_pat_[A-Za-z0-9_]{16,}"), REDACTION),
    # Anthropic, and the generic `sk-` family.
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}"), REDACTION),
    # `sk-` alone is two letters and a hyphen, so unlike the prefixes
    # above it collides with ordinary words: it matches inside
    # `disk-usage-1234567890abcdef` and `risk-score-...`, eating
    # diagnostics. This ONE pattern therefore takes a lookbehind
    # excluding letters and digits — which still allows `_sk-...` and
    # a line start, and refuses the mid-word case. The distinctive
    # prefixes do not need it and must not have it, because that is
    # what reintroduces the glued-token leak.
    (re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}"), REDACTION),
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
    # A password inside a connection string: `postgres://user:pw@host`.
    # The scheme, the user and the host are kept — those are the
    # diagnosis — and only the password goes. Named because this shape
    # is invisible to the key/value rule below: there is no key.
    (
        re.compile(r"([a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+):[^\s/@]+@"),
        r"\1:" + REDACTION + "@",
    ),
    # `Authorization: Bearer <...>` and `Basic <...>`. The scheme is
    # kept; the credential is not.
    #
    # `Token` is NOT in this list, although some APIs use it as a
    # scheme. Case-insensitively it is also the English word, so
    # "the token provided was rejected" became "the token [redacted]
    # was rejected" — destroying a sentence that is the diagnosis.
    # `Authorization: Token ghp_...` is still caught, by the GitHub
    # prefix above and by the key/value rule below.
    (
        re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
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
    # A metric is exempted by its VALUE being a number, not by its name
    # alone. This rule reached its third patch, so the discriminator
    # changed rather than the list growing again:
    #
    #   1. `\bsecret\b` missed `AWS_SECRET_ACCESS_KEY`, because an
    #      underscore is a word character. Boundaries dropped.
    #   2. The widened rule then ate `token_count=8192`, which is
    #      exactly the kind of number that explains a failure. A
    #      metric-suffix exemption was added.
    #   3. That exemption leaked `API_KEY_COUNT=hunter2`, because the
    #      suffix was the whole test.
    #
    # The question the rule is actually answering is "is this value a
    # measurement or a credential", and a measurement is a NUMBER. So
    # the exemption requires both halves: a metric-shaped name AND a
    # numeric value. `token_count=8192` survives; `API_KEY_COUNT=
    # hunter2` is redacted; `password=12345678` is redacted, because
    # no metric suffix matches.
    #
    # Keeping the name list matters for the direction of its failure.
    # It can only ever ADD a redaction (a metric whose suffix nobody
    # listed), never remove one, because the numeric condition does the
    # protecting. The credential side stays open-ended, which is the
    # half that must not be an enumeration.
    #
    # The value takes `{1,}`, not `{4,}`. A four-character minimum let
    # `password=x` and `TOKEN=abc` through unchanged — a short secret
    # is still a secret, and the assignment operator plus a credential
    # name is already the whole evidence that this is one.
    (
        re.compile(
            r"(?i)([A-Za-z0-9_]*(?:api[_-]?key|access[_-]?key|access[_-]?token"
            r"|refresh[_-]?token|auth[_-]?token|secret[_-]?key|client[_-]?secret"
            r"|password|passwd|passphrase|passcode|secret|token|credential"
            r"|pin|otp)[A-Za-z0-9_]*)"
            # A closing quote may sit between the key and the
            # separator: `{"client_secret": "abc"}` is the dominant
            # shape of a worker's stdout, and without it the separator
            # never matched, so every credential in a JSON body was
            # stored verbatim. The backslash covers the ESCAPED form,
            # `{\\"token\\": ...}`, which is how a nested payload
            # arrives inside another JSON string — and that is the
            # shape `claude -p --output-format json` emits.
            #
            # Captured, not discarded, so the quote is put back and the
            # line still reads as JSON.
            r"(\\?[\"']?)(\s*[:=]\s*|\s+is\s+)"
            # The escaped-quoted form comes FIRST, so a value written
            # as \\"secret\\" is taken whole. Without it the bare-token
            # alternative matched the single leading backslash, the
            # backslash was replaced, and the credential was left
            # sitting beside the redaction marker.
            r"(\\\"[^\"\n]{1,}\\\"|\"[^\"\n]{1,}\""
            r"|'[^'\n]{1,}'|[^\s,;}\"'\n]{1,})"
        ),
        _redact_unless_measurement,
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
