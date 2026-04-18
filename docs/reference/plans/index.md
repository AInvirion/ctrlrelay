---
title: Plans
layout: default
parent: Design & history
nav_order: 3
has_children: true
permalink: /reference/plans/
---

# Implementation plans

Phase-by-phase plans for building ctrlrelay. Each plan is written so an agentic
worker (via the `superpowers:executing-plans` or
`superpowers:subagent-driven-development` skill) can execute it task-by-task.

The phases build on each other:

1. **Phase 0** — Python package skeleton, CLI stub, config loader.
2. **Phase 1** — Checkpoint protocol and the skill-audit tooling.
3. **Phase 2** — Telegram bridge for human-in-the-loop prompts.
4. **Phase 3** — Secops pipeline (scheduled GitHub security/PR/issue triage).
5. **Phase 4** — Dev pipeline (self-assigned issue → implementation → PR).

Select a phase from the sidebar to read the full plan.
