# Good Practices Reference

Engineering principles for auditing and fixing code. These are framework-agnostic -- adapt them to whatever stack the project uses.

**These are not a checklist.** Each practice has "Applies when" / "Skip when" conditions. Evaluate them against the project's `{PROJECT_PROFILE}` and ``VID-SPEC.md`` before applying. Flagging violations of practices that don't fit the project is noise, not value.

## How to Use This File

- **Auditors**: read the section(s) tagged for your perspective. Only report violations that are real problems in context.
- **Implementers**: read "Reviewer + Implementer" for direct fixes. When your fix involves refactoring (restructuring concerns, adding abstractions, encapsulating queries), also read "Logic Auditor + Implementer" and follow those practices. Don't refactor for its own sake.
- **Reviewers**: same as implementers, but verify compliance rather than implement. Flag both violations (practice was needed but not applied) and overreach (practice was applied where it shouldn't have been).

## Cardinal Rule — Readability

> "Code is written once but must be read a thousand times."

This maxim governs all code produced or modified by any agent in this pipeline. **Always relevant** — no exceptions.

- **Name things for the reader**: variables, functions, classes, and modules must communicate their purpose to someone who has never seen the codebase. Abbreviations are acceptable only when universally understood in the domain (e.g., `url`, `db`, `http`).
- **Prefer explicit over clever**: a 3-line solution that reads like prose beats a 1-line solution that requires mental compilation. Ternaries, comprehensions, and chained calls are fine when they remain immediately clear; split them the moment they don't.
- **Structure reveals intent**: function length, parameter count, nesting depth, and module organization should all make the code's purpose obvious at a glance. If a reader has to scroll or squint, refactor.
- **Comments explain _why_, code explains _what_**: don't comment obvious operations. Do comment non-obvious decisions, constraints, workarounds, and domain-specific reasoning.
- **Consistency over novelty**: match the existing codebase's idioms, naming conventions, and patterns. Introducing a "better" pattern that only appears once makes the codebase harder to read, not easier.

## Relevant to: Logic Auditor + Implementer

### Separation of Concerns
Each module should have a clear internal structure with distinct layers: persistence, orchestration, business logic, external integrations, and validation. When a single file mixes all of these, it becomes a liability. **Applies when**: the project has multiple modules with non-trivial logic. **Skip when**: simple CRUDs, scripts, or small single-purpose apps where layering adds ceremony without benefit.

### Public Interfaces
Modules should expose a clear entry point (service layer, facade, or similar) that orchestrates internal logic without leaking implementation details. Consumers of a module should never reach into its internals. **Applies when**: the module has external consumers or is part of a larger system. **Skip when**: the module is self-contained with no cross-module callers, or when the entry point would be a trivial pass-through.

### Adapter Pattern for Integrations
External dependencies (APIs, payment providers, messaging services) should be behind an abstract interface with concrete implementations. **Applies when**: there's a realistic chance of swapping providers, or the integration is complex enough that isolating it improves testability. **Skip when**: there will genuinely never be a second provider and the integration is simple -- premature abstraction is not a virtue.

### Domain Objects for Decoupling
Use intermediate data structures (DTOs, dataclasses, typed dicts) to decouple persistence models from business logic. **Applies when**: business logic manipulates data in ways that diverge from the persistence model, or the domain boundary is crossed by multiple consumers. **Skip when**: the object adds no behavior and is identical to the model.

### Domain Exception Hierarchies
Define domain-specific exceptions rather than raising generic ones. Organize them in hierarchies when the domain has distinct error categories that callers handle differently. **Applies when**: the project has multiple error paths that callers need to distinguish. **Skip when**: all errors funnel to the same handler -- deep hierarchies with uniform handling are ceremony.

### Encapsulated Query Logic
Complex or reused queries belong in dedicated query methods (custom managers, repository methods, query builders) rather than scattered inline filters. **Applies when**: the same query pattern appears in multiple places, or the query is complex enough to warrant a name. **Skip when**: one-off simple queries that are clearer inline.

### Transaction-Aware Async Work
When enqueuing async tasks that depend on data written in the current transaction, ensure the task only fires after the transaction commits. **Applies when**: the project uses async tasks (Celery, background jobs, message queues) alongside database transactions. **Not relevant**: in projects without async task queues or without transactional writes.

### Explicit Retry and Queue Policies
Async tasks should declare their retry count, delay, and queue assignment explicitly. **Applies when**: the project has background jobs that can fail transiently. **Skip when**: the project has no async task infrastructure, or tasks are inherently fire-and-forget.

## Relevant to: Security Auditor

### Non-Sequential Identifiers
Public-facing IDs should not expose ordering or count information. **Applies when**: IDs are exposed via API, URLs, or any user-facing interface. **Skip when**: IDs are purely internal and never leave the backend.

### Controlled Database Constraints
Disabling foreign key constraints trades referential integrity for flexibility. Flag instances without clear justification. **Applies when**: the project uses a relational database with cross-table references. **Skip when**: the project uses a document store or has no relational constraints by design.

### Selective Security Scanning
Security tools should be configured to the project's actual risk profile. Verify that critical checks aren't being skipped without coverage from another tool. **Applies when**: the project has security scanning configured. **Flag when**: the project handles sensitive data but has no scanning at all.

### Credential and PII Exposure
Configuration values, API keys, and PII should never appear in code, logs, URLs, or version-controlled files. **Always relevant** -- this one has no "skip when".

## Relevant to: Reviewer + Implementer

### Minimal, Focused Changes
Fixes should be the smallest correct change. Altering return types, function signatures, data structures, or architectural patterns to fix a localized bug is a red flag. **Always relevant** -- regardless of project size.

### Test Quality
Tests should use deterministic data, not randomly generated values that make failures non-reproducible. Test fixtures should be scoped appropriately. **Applies when**: the project has tests. **Adapt when**: the project has no tests -- suggest adding them only if the fix is in critical-path code.

### Code Formatting Enforcement
The project's formatting rules (whatever they are) should be respected by every fix. Run the project's formatter and linter before committing. Don't introduce style changes alongside functional fixes. **Always relevant** -- even if the project has no formal linter, match the existing style.

### PR Standards
PRs should be small enough to review meaningfully. A fix that touches 30 files and 800+ lines is a review burden. **Applies when**: the project uses PR-based workflows. **Adapt when**: batches are large by necessity -- split when possible, document when not.
