"""Tests for `ctrlrelay.core.redact` (#204).

Two properties carry the correctness, and each has a control in both
directions:

- a credential is removed (and a short one is removed *entirely*, so
  the "redact first" test below is not passing because the token fell
  off the end anyway);
- text that is NOT a credential survives, because the whole reason the
  output is kept is to tell a credential failure from a crash, and a
  scrubber that eats commit shas and tracebacks diagnoses nothing.
"""

from ctrlrelay.core.redact import (
    DEFAULT_TAIL_CHARS,
    redact_secrets,
    tail,
    worker_output_tail,
)


class TestRedactSecrets:
    def test_empty_input_gives_empty_output(self) -> None:
        assert redact_secrets("") == ""

    def test_github_classic_token_is_removed(self) -> None:
        secret = "ghp_" + "a" * 36
        out = redact_secrets(f"gh: Bad credentials ({secret})")
        assert secret not in out
        assert "[redacted]" in out
        # The sentence that tells you WHICH failure it was survives.
        assert "Bad credentials" in out

    def test_github_fine_grained_token_is_removed(self) -> None:
        secret = "github_pat_" + "B" * 30
        out = redact_secrets(f"using {secret} now")
        assert secret not in out
        assert "using" in out and "now" in out

    def test_anthropic_key_is_removed(self) -> None:
        secret = "sk-ant-api03-" + "x" * 40
        out = redact_secrets(f"ANTHROPIC_API_KEY={secret}")
        assert secret not in out
        # The key NAME stays: an operator needs to know which setting
        # was wrong, and the name is not the secret.
        assert "ANTHROPIC_API_KEY" in out

    def test_a_token_glued_to_adjacent_output_is_still_removed(self) -> None:
        """No word boundary before the prefix.

        A worker's last bytes are not tidily delimited — a token can
        land flush against surrounding output with no separator. A
        leading `\\b` requires a non-word character there, so the first
        version of this scrubber stored `...contextghp_AAA...` intact.
        """
        secret = "ghp_" + "a" * 36
        assert secret not in redact_secrets(f"context{secret}trailing")

    def test_slack_token_is_removed(self) -> None:
        secret = "xoxb-123456789012-" + "c" * 24
        assert secret not in redact_secrets(f"token {secret}")

    def test_aws_key_id_is_removed(self) -> None:
        secret = "AKIAIOSFODNN7EXAMPLE"
        assert secret not in redact_secrets(f"id={secret}")

    def test_jwt_is_removed(self) -> None:
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        )
        out = redact_secrets(f"Authorization: {jwt}")
        assert jwt not in out
        assert "Authorization" in out

    def test_bearer_credential_goes_and_the_scheme_stays(self) -> None:
        out = redact_secrets("Authorization: Bearer abcdef0123456789xyz")
        assert "abcdef0123456789xyz" not in out
        # Knowing it was a Bearer auth failure is the diagnosis.
        assert "Bearer" in out

    def test_private_key_block_is_replaced_wholesale(self) -> None:
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEAx\nabcdefghijklmnop\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = redact_secrets(f"before\n{pem}\nafter")
        assert "MIIEowIBAAKCAQEAx" not in out
        assert "before" in out and "after" in out

    def test_password_assignment_keeps_the_key_and_drops_the_value(self) -> None:
        out = redact_secrets('password="hunter2hunter2"')
        assert "hunter2hunter2" not in out
        assert "password" in out

    def test_a_namespaced_environment_variable_is_redacted(self) -> None:
        """The normal shape in a worker's output, and it was leaking.

        `\\bsecret\\b` does not match inside `AWS_SECRET_ACCESS_KEY`,
        because an underscore is a word character and so there is no
        boundary before `SECRET`. Measured: every one of these stored
        its value verbatim, while the bare `password=` case passed —
        so the pattern covered the rare spelling and missed the
        common one.
        """
        leaky = {
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "GITHUB_TOKEN": "abcdef1234567890",
            "MY_API_KEY": "abcdef1234567890",
            "X_PASSWORD": "hunter2hunter2",
            "CTRLRELAY_CLIENT_SECRET": "abcdef1234567890",
            # Trailing text after the name, which a trailing `\\b` would
            # also have missed.
            "TOKEN_FOR_GH": "abcdef1234567890",
        }
        for name, value in leaky.items():
            out = redact_secrets(f"{name}={value}")
            assert value not in out, f"{name} leaked its value"
            assert name in out, f"{name} lost its own name"

    def test_a_non_credential_word_ending_in_secret_is_over_redacted(
        self,
    ) -> None:
        """An accepted cost, asserted so it is a decision and not a
        surprise.

        Dropping the word boundaries to catch `AWS_SECRET_ACCESS_KEY`
        also matches `secretary=`. The trade is one-directional: a
        false positive costs one `[redacted]` in diagnostic text, and
        a false negative costs a stored credential. Only one of those
        is recoverable.
        """
        assert "alice" not in redact_secrets("secretary=alice")

    def test_one_assignment_does_not_swallow_the_rest_of_the_line(self) -> None:
        """The value match stops at a comma.

        Without a bounded character class, `token=...` would consume the
        exit code sitting after it — redacting the single most useful
        number in the line.
        """
        out = redact_secrets("token=abcdef123456, exit_code=1, elapsed_ms=3224")
        assert "abcdef123456" not in out
        assert "exit_code=1" in out
        assert "elapsed_ms=3224" in out

    def test_diagnostic_text_is_not_redacted(self) -> None:
        """The negative control on over-redaction.

        Every string here is something an operator needs. A scrubber
        that eats them trades this ticket's defect for a blob.
        """
        text = (
            "Traceback (most recent call last):\n"
            '  File "/home/u/.local/bin/claude", line 8, in <module>\n'
            "FileNotFoundError: [Errno 2] No such file or directory\n"
            "session dev-AInvirion-zzsites-853-9f3c2a11 exit_code 1\n"
            "commit c06fd51001dc8e6281924fb1d5597e42072bc90c\n"
            "elapsed_ms 3224\n"
        )
        assert redact_secrets(text) == text


class TestTail:
    def test_short_text_is_returned_whole_and_unmarked(self) -> None:
        assert tail("short", limit=100) == "short"

    def test_long_text_keeps_the_end_and_is_marked(self) -> None:
        out = tail("A" * 50 + "THE-END", limit=7)
        assert out.endswith("THE-END")
        assert "[...truncated]" in out
        # The head is what gets dropped: the reason a process died is in
        # its last lines, not its banner.
        assert "AAAAAAAA" not in out

    def test_zero_limit_returns_nothing_not_everything(self) -> None:
        """A caller asking for no characters must not be handed them all.

        This is the direction that leaks, so it is asserted rather than
        left to the `len(text) <= limit` comparison, which is TRUE for
        a zero limit and would return the whole input.
        """
        assert tail("secret-bearing output", limit=0) == ""
        assert tail("secret-bearing output", limit=-5) == ""


class TestRedactionHappensBeforeTruncation:
    def test_a_token_straddling_the_cut_leaves_no_fragment(self) -> None:
        """Truncating first would store the token's tail verbatim.

        The token is placed so that the last `limit` characters are
        entirely inside it. Cut-then-redact leaves those characters as
        a literal secret fragment, which no pattern matches any more
        because the `ghp_` prefix was discarded with the head.

        Paired with `test_github_classic_token_is_removed` above, which
        proves the pattern DOES match a whole token — so a pass here
        cannot be explained by the scrubber simply never matching.
        """
        secret = "ghp_" + "B" * 40
        text = "A" * 100 + secret
        limit = 20
        out = tail(redact_secrets(text), limit)
        assert "B" * 20 not in out, (
            "the tail of a credential survived the cap; redaction must "
            "run before truncation"
        )
        assert "[redacted]" in out

    def test_worker_output_tail_composes_them_in_that_order(self) -> None:
        """The test above composes the two calls ITSELF, so it proves a
        property of the test's own arrangement.

        Found by mutation: swapping the order inside
        `worker_output_tail` to `redact_secrets(tail(text, limit))`
        left the test above green, because that test never calls
        `worker_output_tail`. The ordering that ships had no guard —
        the right topic, one hop from its own claim.
        """
        secret = "ghp_" + "B" * 40
        out = worker_output_tail("", "A" * 100 + secret, limit=20)
        assert "B" * 20 not in out, (
            "worker_output_tail truncated before redacting, storing the "
            "tail of a credential verbatim"
        )
        assert "[redacted]" in out


class TestWorkerOutputTail:
    def test_both_streams_empty_gives_empty_string(self) -> None:
        """Not a label with nothing under it.

        "the agent wrote nothing before it died" is a real failure
        shape and must not read like retained output.
        """
        assert worker_output_tail("", "") == ""
        assert worker_output_tail(None, None) == ""
        assert worker_output_tail("   \n", "\t") == ""

    def test_stderr_is_labelled_and_comes_first(self) -> None:
        out = worker_output_tail("out-text", "err-text")
        assert "--- stderr ---" in out
        assert "--- stdout ---" in out
        assert out.index("err-text") < out.index("out-text")

    def test_only_the_stream_with_content_is_labelled(self) -> None:
        out = worker_output_tail("", "just the error")
        assert "--- stderr ---" in out
        assert "stdout" not in out

    def test_secrets_are_scrubbed_from_both_streams(self) -> None:
        a = "ghp_" + "a" * 36
        b = "sk-ant-api03-" + "b" * 40
        out = worker_output_tail(f"stdout has {a}", f"stderr has {b}")
        assert a not in out
        assert b not in out
        assert out.count("[redacted]") == 2

    def test_each_stream_is_capped_independently(self) -> None:
        out = worker_output_tail("X" * 5000, "Y" * 5000, limit=10)
        assert out.count("[...truncated]") == 2
        assert "X" * 11 not in out
        assert "Y" * 11 not in out

    def test_the_default_cap_keeps_the_incidents_payload_whole(self) -> None:
        """#204 measured ~1,252 bytes of output on the failing run.

        The default exists to keep that intact, so it is asserted
        rather than left as a comment nobody checks.
        """
        assert DEFAULT_TAIL_CHARS >= 1252
        payload = "E" * 1252
        out = worker_output_tail("", payload)
        assert payload in out
        assert "[...truncated]" not in out
        # And the cap is ENFORCED, not merely large. Preservation alone
        # passes for a `worker_output_tail` that ignores the limit and
        # returns everything.
        over = worker_output_tail("", "F" * (DEFAULT_TAIL_CHARS + 50))
        assert "[...truncated]" in over
        assert len(over) < DEFAULT_TAIL_CHARS + 50


class TestMetricsSurviveWhileCredentialsDoNot:
    """The scrubber exists so an operator can tell a credential failure
    from a crash. Losing the numbers that explain one is a real cost,
    so both directions are pinned."""

    def test_a_metric_suffix_is_not_redacted(self) -> None:
        for text in (
            "token_count=8192",
            "credential_cache_size=4096",
            "output_token_total=4096",
            "auth_token_ms=1500",
            "secret_bytes=512",
        ):
            assert redact_secrets(text) == text, f"{text} lost its value"

    def test_the_credential_spellings_still_go(self) -> None:
        """The control. Without it, exempting metrics could be
        implemented as "never redact a key/value pair" and the test
        above would still pass."""
        for name, value in (
            ("AWS_SECRET_ACCESS_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCY"),
            ("GITHUB_TOKEN", "abcdef1234567890"),
            ("MY_API_KEY", "abcdef1234567890"),
            ("client_secret", "abcdef1234567890"),
        ):
            out = redact_secrets(f"{name}={value}")
            assert value not in out, f"{name} leaked"

    def test_the_exemption_is_of_metric_names_not_credential_names(
        self,
    ) -> None:
        """A key whose name merely CONTAINS a metric word is not exempt
        unless the name ENDS in one.

        The direction matters: the credential side has to stay
        open-ended because the next secret will have a name nobody
        listed, while a key ending in `_count` holding a secret would
        be perverse. An enumeration is only safe where being incomplete
        costs a redaction rather than a leak.
        """
        out = redact_secrets("token_count_secret=abcdef1234567890")
        assert "abcdef1234567890" not in out


class TestShortValuesAndMetricNames:
    """The key/value rule's two remaining edges, both found by review."""

    def test_a_short_credential_value_is_not_let_through(self) -> None:
        """A four-character minimum let these reach the database.

        A short secret is still a secret, and a credential name plus an
        assignment operator is already the whole evidence that this is
        one.
        """
        for text, value in (
            ("password=x", "x"),
            ("TOKEN=abc", "abc"),
            ("api_key=1", "1"),
        ):
            out = redact_secrets(text)
            assert "[redacted]" in out, f"{text} was not redacted"
            assert not out.endswith(value), f"{text} leaked its value"

    def test_a_credential_name_ending_in_a_metric_suffix_is_redacted(
        self,
    ) -> None:
        """The metric exemption needs BOTH halves.

        Exempting on the name alone leaked `API_KEY_COUNT=hunter2`,
        because the suffix was the whole test. A measurement is a
        NUMBER, so the value has to be one too.
        """
        for text, secret in (
            ("API_KEY_COUNT=hunter2", "hunter2"),
            ("token_count=ghp_abcdefghijklmnopqrstuvwx", "ghp_abcdef"),
            ("secret_total=abcdef", "abcdef"),
            # Not a number, even though it starts with digits.
            ("token_size=8192abc", "8192abc"),
        ):
            out = redact_secrets(text)
            assert secret not in out, f"{text} leaked"

    def test_a_metric_with_a_numeric_value_still_survives(self) -> None:
        """The control for the test above. Without it, "always redact"
        passes it and the metrics are lost again."""
        for text in (
            "token_count=8192",
            "auth_token_ms=1500",
            "secret_bytes=-1",
            "credential_cache_size=1.5e3",
        ):
            assert redact_secrets(text) == text, f"{text} lost its value"
