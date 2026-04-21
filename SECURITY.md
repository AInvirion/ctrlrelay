# Security Policy

## Supported versions

Only the latest `0.1.x` release receives security fixes while the project is in
alpha. After a stable `1.0` release this policy will expand.

## Reporting a vulnerability

**Please do not file public GitHub issues for security vulnerabilities.**

Instead, open a [private security advisory][advisories] on GitHub. That gives
us a private channel to triage the report and lets you get credit in the
published advisory when we release a fix.

If you cannot use GitHub for any reason, email
`security@ainvirion.com` with the subject line
`ctrlrelay: security report`.

We aim to acknowledge reports within **72 hours** and ship a fix or
mitigation within **14 days** for high-severity issues.

## Scope

In-scope:

- The `ctrlrelay` CLI and its pipelines (`dev`, `secops`, scheduled jobs).
- The Telegram bridge and socket transport.
- Anything in `src/` and `config/`.

Out-of-scope:

- Vulnerabilities in upstream dependencies (`typer`, `pydantic`, `apscheduler`,
  `python-telegram-bot`, etc.) — report those to their respective projects.
- Misuse scenarios where the attacker already has local shell access on the
  machine running the daemon (the threat model assumes a single-user,
  local-first install).
- The headless coding-agent CLI (today: Claude Code), the `gh` CLI,
  and `git` — those are external tools we shell out to; report issues
  to their respective projects.

## Credential handling

As of `v0.1.4`, the Telegram bot token is passed to the daemon child via the
environment (inherited through `subprocess.Popen`), not via command-line
arguments — so it does not appear in `ps` / `/proc/*/cmdline`. If you are
upgrading from an earlier pre-release build that predates this change and you
ran it in a shared environment, rotate your bot token.

[advisories]: https://github.com/AInvirion/ctrlrelay/security/advisories/new
