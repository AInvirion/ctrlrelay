# dev-sync

Local-first orchestrator that drives headless Claude Code (`claude -p`)
across your GitHub repos. Watches for assigned issues, runs the dev pipeline
in an isolated git worktree, opens a PR, and asks you on Telegram when it
gets stuck.

📖 **Docs:** <https://ainvirion.github.io/dev-sync/>

## Install

Requires Python 3.12+, the `claude` CLI, the `gh` CLI, and `git` 2.20+.

```bash
git clone https://github.com/AInvirion/dev-sync.git
cd dev-sync

# With uv (recommended):
uv pip install -e .

# Or with pip:
pip install -e .
```

## Quick start

```bash
# Copy and edit the example config:
cp config/orchestrator.yaml.example config/orchestrator.yaml

# Validate it:
dev-sync config validate

# Run the dev pipeline against an issue you're assigned:
dev-sync run dev --issue 42 --repo your-org/your-repo

# Or start the poller to auto-process newly assigned issues:
dev-sync poller start --interval 300
```

For everything beyond this — the full config schema, Telegram setup, the
checkpoint protocol, running as a launchd/systemd service, the architecture,
and contributing — see the documentation site:

- [Getting started](https://ainvirion.github.io/dev-sync/getting-started/)
- [Configuration](https://ainvirion.github.io/dev-sync/configuration/)
- [Telegram bridge](https://ainvirion.github.io/dev-sync/bridge/)
- [Feedback loop](https://ainvirion.github.io/dev-sync/feedback-loop/)
- [CLI reference](https://ainvirion.github.io/dev-sync/cli/)
- [Operations](https://ainvirion.github.io/dev-sync/operations/)
- [Architecture](https://ainvirion.github.io/dev-sync/architecture/)
- [Development](https://ainvirion.github.io/dev-sync/development/)

## License

MIT
