---
title: Design & history
layout: default
nav_order: 10
has_children: true
permalink: /reference/
description: "Original design specs and the phase-by-phase plans dev-sync was built from. Kept for context — not the right entry point if you just want to use the tool."
---

# Design & history

These pages predate the operator documentation. They describe how dev-sync was
designed and the implementation plans that built it phase-by-phase.

If you're trying to **use** dev-sync, start with
[Getting started]({{ '/getting-started/' | relative_url }}). If you're trying
to **understand the codebase**, the architecture page covers the same ground
in less detail and is more current — see
[Architecture]({{ '/architecture/' | relative_url }}).

What's in here:

- **[Orchestrator build spec]({{ '/reference/orchestrator-spec/' | relative_url }})** —
  the original design doc the orchestrator was built from. Goals, non-goals,
  pipelines, Telegram bridge contract, and the dispatcher shape. Some details
  have drifted from the implementation; treat it as historical intent.
- **[Claude Code project guide]({{ '/reference/claude-code-project-guide/' | relative_url }})** —
  generic Claude Code project-development guidance that informed dev-sync's
  prompt design. Useful background, not project-specific.
- **[Plans]({{ '/reference/plans/' | relative_url }})** — the five
  implementation phases (Phase 0 → Phase 4) the project was built in.
- **[Specs]({{ '/reference/specs/' | relative_url }})** — the orchestrator
  implementation design, the bridge between the build spec and the actual
  layered architecture.
