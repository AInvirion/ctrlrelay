---
title: Personalization sync
layout: default
nav_order: 8
description: "Sync your Claude Code config, per-project memory, and spec/superpower outputs across machines via a private GitHub repo, without contaminating any project's source tree."
permalink: /personalization/
---

# Personalization sync

Cross-machine sync of the operator's Claude Code state — global config,
per-project memory, spec/superpower outputs, and workspace planning notes —
through a separate (typically private) GitHub repo. ctrlrelay clones that
repo into a checkout, lays down symlinks at the right places, and rebases
onto a per-machine branch so two computers pushing concurrently never
overwrite each other's deltas.

The personalization repo is **not** the same as the project repos in
`repos:`. It's an out-of-tree store for things that survive across sessions
and machines but don't belong inside any project's source tree.

## Why this exists

Without sync, the operator hits three problems:

1. **`~/.claude/CLAUDE.md`, skills, agents, commands** drift between
   machines. Edits on the laptop don't reach the workstation.
2. **Per-project memory under `~/.claude/projects/<encoded>/memory/`**
   is lost the moment you switch machines. Claude has to relearn the
   project from scratch every time.
3. **Spec / superpower outputs** that Claude writes per project would be
   either lost (if kept under `~/`), polluting (if committed inside the
   project repo), or invisible to other machines.

A second git repo solves all three cleanly. The cost is one private repo
per operator, plus a `personalization:` block in `orchestrator.yaml`.

## Layout: the dotclaude repo

The repo can be named anything, but the convention is `<your-handle>/dotclaude`
or similar. The example layout this doc assumes:

```
dotclaude/
├── global/
│   ├── CLAUDE.md          # ~/.claude/CLAUDE.md
│   ├── skills/            # ~/.claude/skills/
│   ├── agents/            # ~/.claude/agents/
│   ├── commands/          # ~/.claude/commands/
│   └── keybindings.json
├── claude-memory/
│   └── <owner>--<repo>/   # ~/.claude/projects/<encoded>/memory/
└── specs/
    └── <owner>--<repo>/   # <project-parent>/specs/<owner>--<repo>/
```

The `<owner>--<repo>` slug uses a **double hyphen** as the owner/repo
separator so `a-b/c` and `a/b-c` produce different slugs (`a-b--c` vs.
`a--b-c`) and never collide.

**Why a directory next to the project for specs**, not inside it: the
project's own git history must stay uncontaminated. Spec docs Claude
writes during development belong to your operator state, not to the
project's commit log.

## One-time setup

### 1. Create the personalization repo

Create an **empty private** GitHub repo. The `gh` CLI is fine:

```bash
gh repo create your-handle/dotclaude --private --description \
  "Cross-machine sync of Claude Code personalization (config, memory, specs) — managed by ctrlrelay"
```

Don't add a README, .gitignore, or license — ctrlrelay's `init` will
populate it.

### 2. Configure `personalization:` in `orchestrator.yaml`

```yaml
personalization:
  repo: "your-handle/dotclaude"
  # checkout_path: "~/.ctrlrelay/personalization"   # default
  # main_branch: "main"                              # default
  # node_id: "studio-mac"                            # default: top-level node_id
  paths:
    - source: "global/CLAUDE.md"
      target: "~/.claude/CLAUDE.md"
    - source: "global/skills/"
      target: "~/.claude/skills/"
    - source: "global/agents/"
      target: "~/.claude/agents/"
    - source: "global/commands/"
      target: "~/.claude/commands/"

    # Per-project memory. Placeholders resolve from each repo's
    # local_path at link time, so the same config works on machines
    # with different home directories or repo layouts.
    - source: "claude-memory/${PROJECT}/"
      target: "~/.claude/projects/${PROJECT_ENCODED}/memory/"
      project_scoped: true

    # Spec / superpower outputs Claude writes per project, kept
    # NEXT TO the repo (not inside it).
    - source: "specs/${PROJECT}/"
      target: "${PROJECT_PARENT}/specs/${PROJECT}/"
      project_scoped: true
```

Trailing slashes matter: `source: "global/CLAUDE.md"` is treated as a
file, `source: "global/skills/"` is treated as a directory. The target
must agree — a mismatch is rejected with `skipped-target-type-mismatch`.

#### Path placeholders

Use these in `source` and `target`. `${PROJECT_*}` placeholders only
work when `project_scoped: true`.

| Placeholder | Resolves to |
|---|---|
| `${HOME}` | Current user's home (target only) |
| `${PROJECT}` | `<owner>--<repo>` flat slug |
| `${PROJECT_ENCODED}` | Claude's path encoding of the repo's `local_path` |
| `${PROJECT_LOCAL}` | Absolute path of the repo's local checkout |
| `${PROJECT_PARENT}` | Parent dir of `${PROJECT_LOCAL}` |

`${PROJECT_ENCODED}` matches the way Claude Code itself encodes paths
into `~/.claude/projects/`: `/Users/foo/Projects/bar` →
`-Users-foo-Projects-bar`. ctrlrelay computes it from each repo's
`local_path` so the same config works across machines with different
home directories.

### 3. Run `init`

```bash
ctrlrelay personalization init
```

This:

1. Clones `your-handle/dotclaude` into `~/.ctrlrelay/personalization`.
2. Creates a per-machine working branch named `personalization/<node_id>`
   (or fast-forwards to it if it already exists on the remote).
3. Walks the `paths` list and lays down symlinks. **Adopt-flow** is on
   by default: when a target like `~/.claude/CLAUDE.md` already exists
   as a real file but the corresponding source slot in the repo is
   empty, the existing target is moved into the repo and a symlink is
   wired in its place. Your content is preserved and immediately
   under sync.

Pass `--no-adopt` to opt out of adoption and keep the conservative
"refuse to touch any pre-existing real path" behavior:

```bash
ctrlrelay personalization init --no-adopt
```

With `--no-adopt`, pre-existing real targets surface as
`skipped-real-file-at-target` and you back them up + remove them
before re-running `init`.

After `init`, run `push` once so the adopted content reaches GitHub:

```bash
ctrlrelay personalization push -m "initial personalization import from <hostname>"
```

## Day-to-day commands

### `personalization status`

```bash
ctrlrelay personalization status
```

Prints the working branch, repo URL, ahead/behind counts vs. origin,
and the per-symlink state — `correct`, `wrong-target`, `missing`,
`source-missing`, etc. — without touching the filesystem.

### `personalization push`

```bash
ctrlrelay personalization push -m "added a new skill"
```

Stages everything inside the allowlist (the entries declared in
`paths`), commits, rebases the per-machine branch onto `origin/main`,
and pushes. If `origin/main` advanced between your fetch and your
push, the FF is retried up to three times so concurrent edits from
your other machine don't strand your commit.

`--force-with-lease` is used on the per-machine branch when the local
working branch has diverged from `origin/<working_branch>` (i.e. the
rebase rewrote commits the remote already had). Per-machine branches
are owned by exactly one node by design, so the lease almost always
passes; if a stray update slips in, the push fails safely instead of
clobbering it.

### `personalization pull`

```bash
ctrlrelay personalization pull
```

Fetches, rebases your per-machine branch onto `origin/main`, fast-
forwards your local `main` if it's a strict ancestor of the remote,
and re-wires symlinks (the config-as-code shipped in the repo may have
changed). Conflicts during the rebase abort cleanly and list the
unmerged files — resolve in the checkout, then re-run.

### Auto-pull on cron

Set `schedules.personalization_cron` to converge machines without
manual sync:

```yaml
schedules:
  secops_cron: "0 6 * * *"
  personalization_cron: "*/15 * * * *"   # every 15 min
```

The poller registers an APScheduler job that runs `personalization pull`
on this schedule. Two safety rails:

- **Skip-on-dirty.** If the working tree has uncommitted changes, the
  auto-pull skips with a `working tree dirty` summary. A daemon
  rebasing under your unsaved edits is exactly the surprise we don't
  want.
- **No adoption.** Adoption is an init-time concern. The auto-pull
  re-wire phase runs with `adopt=False` so a background sync never
  silently moves files. New entries in `paths` that have no remote
  source yet stay as `skipped-source-missing` until you `init` again.

Auto-push is intentionally **not** scheduled. A daemon committing on
the operator's behalf without explicit intent is the kind of thing
that surprises people; commits stay manual.

## Multi-machine bootstrap

On the second machine:

1. Install ctrlrelay, copy your `orchestrator.yaml` over (the
   `personalization:` block must be identical except for `node_id` if
   you set it explicitly).
2. `ctrlrelay personalization init`.
3. Done. The clone fast-forwards `personalization/<this-machine>` from
   `origin/main`, lays the symlinks down on top of your existing
   `~/.claude/` (adopting where empty, refusing where both sides have
   real content), and you're synced.

If the second machine already has a populated `~/.claude/CLAUDE.md`
that differs from the one on the first machine, you'll get
`skipped-conflict-both-exist` for that path. Pick a winner manually
(diff, copy the chosen content into the repo source, delete the
loser's local), then re-run `init`.

## Per-machine branches and the FF dance

Each machine commits to `personalization/<node_id>`. `push` rebases that
branch onto `origin/main`, then fast-forwards `origin/main` from the
per-machine branch. Two machines pushing concurrently:

```
A: main = X
A: personalization/a = X + a1
A: push  -> rebases on X, ff main: main = X + a1

B: main = X (stale)
B: personalization/b = X + b1
B: push  -> fetch sees main = X + a1
         -> rebases b1 on top: main + b1
         -> ff main: main = X + a1 + b1   (succeeds, retry not needed)
```

If both machines fetch at the exact same instant and both push, only
one wins the FF; the other retries up to three times. No force-push to
`main` is ever attempted.

## Gotchas

### Worktrees inside the personalization checkout

Don't create git worktrees inside `~/.ctrlrelay/personalization`. The
checkout is a single working copy of a single per-machine branch.
Worktrees would attach to the same `.git` and confuse `status` /
`pull`. ctrlrelay's per-repo worktrees (`paths.worktrees`) are an
unrelated, project-scoped feature.

### Editing through the symlink is editing the synced repo

A write to `~/.claude/CLAUDE.md` is a write to
`~/.ctrlrelay/personalization/global/CLAUDE.md`. After editing, run
`ctrlrelay personalization push`. If you forget, the auto-pull's
skip-on-dirty rail keeps things safe — the daemon won't rebase under
your unsaved work — but your edit doesn't reach other machines until
you push.

### Allowlist enforcement

Only the entries declared in `paths` are staged on `push`. Random
files dropped into the checkout (a stray `.DS_Store`, a temp note)
are not committed. This is intentional: the personalization repo is
the operator's, not Claude's, and "everything that's there"
is not the right granularity.

### Conflict during auto-pull

If a scheduled auto-pull hits a rebase conflict (someone pushed an
edit to the same file from another machine while a local commit was
pending), the rebase aborts and the conflict files appear in the
poller log. Resolve manually with `ctrlrelay personalization pull`,
which surfaces the same files in your terminal.

### Repo URL match is strict

`init` verifies the existing checkout's `origin` URL is exactly
`github.com:<your-handle>/<repo>` before treating it as "ours" for
re-init. A mismatch refuses to proceed — back up or remove the
checkout before running `init`.

## Privacy

The personalization repo holds your Claude memory, skills, and per-
project notes. Treat it like any other operator-private store:

- **Make the repo private.** `gh repo create --private`.
- **Don't put secrets in `~/.claude/CLAUDE.md`** that you wouldn't put
  in any other git repo. Auth tokens, API keys, etc. belong in
  environment variables, not in synced markdown.
- ctrlrelay never reads, transforms, or transmits the contents itself
  beyond `git push` / `git pull` to the URL you configured. There is
  no telemetry on personalization.

## Disabling

Remove the `personalization:` block from `orchestrator.yaml` and
restart the poller. Existing symlinks remain on disk pointing into
`~/.ctrlrelay/personalization`. To revert a target back to a real file,
copy through the symlink first (`cp ~/.claude/CLAUDE.md /tmp/x &&
rm ~/.claude/CLAUDE.md && mv /tmp/x ~/.claude/CLAUDE.md`). The
checkout itself can be deleted once nothing points into it.
