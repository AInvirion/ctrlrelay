# Contributing to ctrlrelay

Thank you for your interest in contributing to ctrlrelay! We welcome
contributions from the community.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [How to Contribute](#how-to-contribute)
- [Development Workflow](#development-workflow)
- [Coding Standards](#coding-standards)
- [Commit Guidelines](#commit-guidelines)
- [Pull Request Process](#pull-request-process)
- [Contributor Assignment Agreement](#contributor-assignment-agreement)

## Code of Conduct

By participating in this project, you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md). We expect all contributors to:

- Be respectful and constructive.
- Welcome newcomers and help them get started.
- Focus on what is best for the community.
- Show empathy towards other community members.

## Getting Started

1. **Fork the repository** on GitHub.
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/your-username/ctrlrelay.git
   cd ctrlrelay
   ```
3. **Add upstream remote**:
   ```bash
   git remote add upstream https://github.com/AInvirion/ctrlrelay.git
   ```
4. **Install dependencies** (Python 3.12+ required):
   ```bash
   uv sync --all-extras     # or: pip install -e ".[dev]"
   ```
5. **Create a branch** for your work:
   ```bash
   git checkout -b feature/your-feature-name
   ```

## How to Contribute

### Reporting Bugs

Before creating a bug report:

- Check the [existing issues](https://github.com/AInvirion/ctrlrelay/issues)
  to avoid duplicates.
- Collect version, OS, Python interpreter, error message, and steps to
  reproduce.

File via the `Bug report` issue template — it prompts for the fields
we need most.

### Suggesting Enhancements

Enhancement suggestions are welcome! Please:

- Check existing issues and discussions first.
- Explain the use case — what are you trying to do, and why doesn't
  today's ctrlrelay let you do it?
- Consider backward compatibility — this project is in alpha, but
  operator-facing changes still need a migration path.

### Code Contributions

We welcome code contributions for:

- Bug fixes
- New features (scheduled jobs, transports, pipelines)
- Performance improvements
- Documentation improvements
- Test coverage

## Development Workflow

1. **Sync with upstream**:
   ```bash
   git fetch upstream
   git checkout main
   git merge upstream/main
   ```

2. **Create a feature branch**:
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make your changes** following the coding standards below.

4. **Write tests** for new functionality. The suite uses
   `pytest` + `pytest-asyncio`; see existing tests under `tests/`
   for patterns.

5. **Run tests** and **lint**:
   ```bash
   uv run pytest
   uv run ruff check src tests
   ```

6. **Commit** following commit guidelines (below).

7. **Push** to your fork and **open a PR** on GitHub. The CLA bot
   will comment with instructions for signing the CLA if this is your
   first contribution.

## Coding Standards

### General Guidelines

- Write clear, readable, maintainable code. Prefer short, named
  functions over long ones and obvious structure over clever tricks.
- Follow existing code style and patterns — if you're not sure, look
  at nearby code.
- Add comments for the **why**, not the **what**. A comment should
  explain a non-obvious constraint, workaround, or invariant.
- Keep functions small and focused.

### Python Standards

- Target Python 3.12+. Use modern syntax (`X | None`, `list[str]`,
  `async def` over callbacks, etc.).
- Type-annotate function signatures and class attributes.
- Use `pydantic` for config / external input, dataclasses for
  internal structures.
- Keep I/O (subprocess, file, network) in well-defined modules so
  the core remains testable without those dependencies.

### Testing

- Write unit tests for new functionality; use mocks for `subprocess`,
  `gh` / `git` CLI, and network calls.
- Integration tests that actually shell out are welcome for
  complex pipeline paths — keep them under `tests/test_*_integration.py`
  so they're easy to skip in CI if needed.
- Ensure all tests pass locally before submitting a PR.
- Maintain or improve test coverage.

## Commit Guidelines

We follow the [Conventional Commits](https://www.conventionalcommits.org/)
specification:

```
<type>(<scope>): <subject>

<body>

<footer>
```

### Types

- **feat**: New feature
- **fix**: Bug fix
- **docs**: Documentation changes
- **style**: Code style changes (formatting, etc.)
- **refactor**: Code refactoring
- **perf**: Performance improvements
- **test**: Adding or updating tests
- **chore**: Maintenance tasks (deps, release, tooling)
- **ci**: CI configuration / workflow changes

### Examples

```
feat(scheduler): add weekly activity summary job

Register a new cron job on the in-process scheduler that writes a
markdown summary of the week's merged PRs to ~/.ctrlrelay/reports/.

Closes #142
```

```
fix(poller): un-mark issue on lock-conflict so it retries next poll

Previously a dev-pipeline failure during a scheduled secops sweep
silently dropped the issue. Detect the lock-conflict error in
handle_issue and call IssuePoller.unmark_seen so the next poll
re-queues it.

Fixes #128
```

## Pull Request Process

1. **Update documentation** if your change affects user-visible
   behavior — `README.md`, `docs/`, or the CLI reference.
2. **Add tests** for new functionality.
3. **Ensure CI passes** (all tests and linting).
4. **Update `CHANGELOG.md`** under the `[Unreleased]` section.
5. **Request review** from maintainers — PRs are reviewed and
   merged by squash-merge.
6. **Address feedback** promptly and professionally.
7. **Sign the CAA** on your first PR (see
   [Contributor Assignment Agreement](#contributor-assignment-agreement)).
   The bot will comment with instructions.

### PR Checklist

- [ ] Code follows project style guidelines
- [ ] Tests added/updated and passing
- [ ] Documentation updated
- [ ] Commits follow Conventional Commits
- [ ] No breaking changes (or documented in CHANGELOG)
- [ ] `CHANGELOG.md` updated

## Contributor Assignment Agreement

AInvirion projects use a Contributor Assignment Agreement (CAA) rather
than a plain Contributor License Agreement. By contributing you agree
to assign intellectual property rights in your contribution to
AInvirion LLC; your contribution is then published under the
Apache License 2.0.

The full agreement text lives in [this gist][caa-gist].

On your first pull request, the CLA Assistant bot posts a comment
with signing instructions. Reply with the exact phrase it prompts for
("I have read the Contributor Assignment Agreement and I hereby
accept the terms.") and the bot records your signature — one-time,
one-comment, per contributor.

[caa-gist]: https://gist.github.com/oscarvalenzuelab/1d635e89f93b86338985b7cebb1596ac

## Questions?

If you have questions about contributing, please:

- Check existing documentation and issues.
- Ask in GitHub Discussions.
- Contact us at `contact@ainvirion.com`.

---

Thank you for contributing to ctrlrelay!
