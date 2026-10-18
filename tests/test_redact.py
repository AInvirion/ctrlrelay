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
