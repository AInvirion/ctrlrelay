---
title: Development
layout: default
nav_order: 9
description: "Local development setup, running tests, linting, and contributing."
permalink: /development/
---

# Development

## Local setup

```bash
git clone https://github.com/AInvirion/ctrlrelay.git
cd ctrlrelay

# Editable install with dev extras (uv recommended):
uv pip install -e '.[dev]'

# Or with pip:
pip install -e '.[dev]'
```

This installs `pytest`, `pytest-cov`, `pytest-asyncio`, and `ruff` alongside
the runtime dependencies.

Confirm the CLI is wired up:

```bash
ctrlrelay --version
```

## Running tests

```bash
# All tests:
pytest

# Verbose, with names:
pytest -v

# A single file:
pytest tests/test_dev_pipeline.py -v

# A single test:
pytest tests/test_dev_pipeline.py::test_blocked_then_resume -v

# With coverage:
pytest --cov=src/ctrlrelay --cov-report=term-missing
```

`pyproject.toml` sets `pythonpath = ["src"]` and `testpaths = ["tests"]` so
pytest finds the package without an extra install step on a fresh checkout.

The Telegram bridge tests stub the network — no real bot token is required to
run the suite. Tests that exercise the dev / secops pipelines use the
`file_mock` transport so they stay hermetic.

## Linting

```bash
# Check:
ruff check src tests

# Auto-fix what's safe:
ruff check --fix src tests

# Format (imports, whitespace):
ruff format src tests
```

The ruff config in `pyproject.toml`:

- Line length: 100
- Target: Python 3.12
- Selected rules: `E`, `F`, `I`, `N`, `W`

Both linting and tests should pass before opening a PR.

## Validating docs

The docs site under `docs/` is a [Jekyll](https://jekyllrb.com/) build with
the [just-the-docs](https://just-the-docs.com/) remote theme. There is a
structural test suite that catches misconfigured pages before GitHub Pages
builds them:

```bash
pytest tests/test_docs_site.py -v
```

It checks:

- `_config.yml` is valid YAML and declares the `remote_theme`.
- Every Markdown page has `title` front matter.
- Every page declared as `parent: X` exists with `has_children: true`.
- Sibling pages have unique `nav_order`.

You can also build the site locally if you have Ruby installed:

```bash
cd docs
bundle install         # one-time
bundle exec jekyll serve
# Open http://localhost:4000/ctrlrelay/
```

## Project layout

```text
ctrlrelay/
├── src/ctrlrelay/                # Python package (orchestrator core)
│   ├── cli.py                   # Typer CLI entry point
│   ├── core/                    # Dispatcher, worktree, state, config, ...
│   ├── pipelines/               # dev, secops, post_merge
│   ├── bridge/                  # Telegram bridge daemon
│   ├── transports/              # SocketTransport, FileMockTransport
│   └── dashboard/               # Optional dashboard push client
├── tests/                       # pytest suites (one file per module)
├── config/                      # Example orchestrator.yaml
├── docs/                        # This Jekyll site
├── scripts/                     # Shell helpers (./sync wrapper, manifest)
├── claude-config/               # Git-tracked Claude Code config (export/import)
├── codex-config/                # Git-tracked Codex CLI config
└── mcp-servers/                 # MCP servers (codex-reviewer, ...)
```

## Contributing

1. **Open an issue first** for anything beyond a typo or trivial fix. The
   `ctrlrelay` poller can pick assigned issues up automatically — see
   [Getting started]({{ '/getting-started/' | relative_url }}).
2. **Branch naming.** Use `fix/issue-{n}` (the dev-pipeline default) so the
   issue ↔ branch ↔ PR linkage stays clean.
3. **TDD.** Write or extend tests for any behavior change. Pipelines have
   integration tests under `tests/test_*_pipeline.py`; the bridge has its
   own asyncio-based suite.
4. **Lint and test before pushing.** CI runs both; pre-push catches things
   faster.
5. **Keep PRs small.** A single PR per logical change makes review tractable
   and lets the post-merge handler keep the issue / PR / branch lifecycle
   clean.
6. **Don't commit secrets.** Bot tokens and dashboard tokens belong in
   environment variables — `transport.telegram.bot_token_env` and
   `dashboard.auth_token_env` only hold the variable _name_.

## CI

Two GitHub Actions workflows live under `.github/workflows/`:

- **`build.yml`** — runs on push to `main`, on tag pushes (`v*`), and on PRs.
  Builds the sdist and wheel via `uv build`. On tags / releases, attaches the
  artifacts to the GitHub release.
- **`pages.yml`** — runs on push to `main` when anything under `docs/`
  changes. Builds the Jekyll site and deploys to GitHub Pages.

Both must pass before a PR is mergeable.
