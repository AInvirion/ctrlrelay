---
title: Home
layout: default
nav_order: 1
description: "dev-sync — local-first orchestrator for Claude Code."
permalink: /
---

# dev-sync documentation
{: .fs-9 }

Local-first orchestrator that wraps headless Claude Code (`claude -p`) to run
secops and dev pipelines across multiple GitHub repositories, with Telegram as
the human-in-the-loop channel and a lightweight dashboard for heartbeats.
{: .fs-6 .fw-300 }

[Get the code on GitHub](https://github.com/AInvirion/dev-sync){: .btn .btn-primary .fs-5 .mb-4 .mb-md-0 .mr-2 }
[Orchestrator spec]({{ '/dev-sync-orchestrator-spec' | relative_url }}){: .btn .fs-5 .mb-4 .mb-md-0 }

---

## What lives here

This site is the canonical home for dev-sync design docs, operator guides, and
implementation plans. It is generated from the Markdown under `docs/` in the
[AInvirion/dev-sync](https://github.com/AInvirion/dev-sync) repository.

### Start here

- **[Claude Code Project Guide]({{ '/Claude_Code_Project_Guide' | relative_url }})** —
  how to direct Claude Code effectively on this project.
- **[Orchestrator Spec]({{ '/dev-sync-orchestrator-spec' | relative_url }})** —
  the build spec for the orchestrator itself (goals, architecture, pipelines).

### Dig deeper

- **[Plans]({{ '/superpowers/plans/' | relative_url }})** — phase-by-phase
  implementation plans (Phase 0 → Phase 4).
- **[Specs]({{ '/superpowers/specs/' | relative_url }})** — design documents
  and architectural references.

---

## Repository layout

```text
dev-sync/
├── docs/                 # This site (Jekyll, GitHub Pages)
├── src/dev_sync/         # Python package (orchestrator core)
├── tests/                # pytest suites
├── scripts/              # Shell helpers (device setup, manifest, sync)
├── claude-config/        # Git-tracked Claude Code config (export/import)
├── codex-config/         # Git-tracked Codex CLI config and skills
└── mcp-servers/          # MCP servers (e.g. codex-reviewer)
```

See the [README](https://github.com/AInvirion/dev-sync#readme) for CLI usage
and sync workflows.
