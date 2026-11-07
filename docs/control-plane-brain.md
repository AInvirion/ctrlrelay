---
title: Control plane as the brain (design)
layout: default
nav_order: 13
description: "Proposal: how the control plane holds knowledge, memory, roles and credentials for every node, so a node can start clean, and how that is kept safe."
permalink: /control-plane-brain/
---

# The control plane as the brain: design

**Status: proposed, not agreed, not built.** This is issue #372. It builds on
[Registered mode]({{ '/registered-mode/' | relative_url }}) (#251), which
fixes the protocol between a node and the control plane. That page assumes
the control plane never puts text in front of an agent and never holds a
credential. This page drops that assumption, on purpose, and says what
replaces it. The choices still open are at the end, under "Open choices".

## The idea in one paragraph

The control plane holds what the factory **knows** (context, learnings,
decisions, history), what each kind of work **is** (roles, with their
instructions, skills and agent settings) and, where a role needs it, the
**credentials** for that work. A node holds the **runtime**: ctrlrelay, the
agent CLI and its login, the sandbox, its own GitHub token and its key. A
node with an empty cache registers, takes an assignment, fetches what that
assignment names, and runs. Losing a node loses no knowledge.

**The safety rule that makes this possible: anything that changes what an
agent is told, or what it can reach, is signed by the operator, with a key
the control plane server does not hold.** The control plane stores and hands
out that content; it cannot author it. Everything else it sends is data, and
a node shows data to an agent only as data.

## What exists today, and what it becomes

| Today, on each node | Becomes |
|---|---|
| `<paths.contexts>/<owner-repo>/CLAUDE.md`, linked into each worktree | **Repo context**, a signed knowledge entry per repo |
| Skills under `paths.skills`, synced by [personalization]({{ '/personalization/' | relative_url }}) | Skills inside a signed **role** bundle |
| `agent:` block in each node's config (type, model, sandbox paths) | Agent settings inside a role, within the node's own limits |
| Per-repo learning (#225), planned in the repo on GitHub (decision 5 of #251) | **Learnings**, proposed by nodes, approved and signed by the operator |
| Plans saved under the contexts directory (#220) | **History**: plans, run summaries and outcomes, stored as data |
| Third-party secrets in the node's environment | **Role credentials**, sealed to one node for one assignment |

Personalization sync and the contexts directory keep working in standalone
mode. A registered node with brain content enabled ignores them for runs
that carry a role.

## Three kinds of content

Everything the control plane holds falls in one of three kinds. The kind
decides how it is protected and how an agent sees it.

| Kind | Examples | Signed by the operator | How the agent sees it |
|---|---|---|---|
| **Instructions** | roles, skills, repo context, approved learnings, decisions, config | yes | as instructions, in the prompt or as skill files |
| **Data** | run history, plans, summaries of earlier runs, proposed learnings, numbers | no | inside a fenced block marked as data, never as instructions, like issue text today |
| **Secrets** | role credentials | no (sealed instead, see "Credentials") | never: only the step that needs one gets it, as an environment variable |

The protocol content from #251 (assignments, commands, leases) is data too:
it says **what** to work on, never **how**.

### Signing

- The operator holds an **operator key** (Ed25519) off the control plane
  server: on a hardware key or the operator's own machine.
- The node pins the operator's public keys in its **local** `control_plane`
  block, which the control plane can never change (decision 6 of #251).
- Every instruction item is a **bundle**: a JSON manifest plus files, named
  by the SHA-256 of its content, with `name`, `version`, `not_after` and the
  operator's signature over all of it.
- A node refuses a bundle that is unsigned, signed by a key it does not pin,
  past `not_after`, or older than a version of the same name it has already
  used (no rollback). A refused bundle fails the assignment with
  `bundle_refused`, which the node reports and posts in chat. It never runs
  without it.
- Revoking a bundle is a signed revocation list, fetched with the heartbeat.
  An unreachable list is "could not check", not "nothing revoked": the node
  keeps using what it has until `not_after`, and takes no new role it cannot
  check.

Signing is the approval step. The operator approves a role change, a context
edit or a learning in the control plane's UI, the UI asks the operator's key
to sign it, and only then does it become something a node will use.

## Knowledge and memory

**What is stored, per repo:**

- **Context:** what `CLAUDE.md` in the contexts directory holds today: how
  the repo is built and tested, its rules, its layout. Instructions.
- **Learnings:** facts from real runs (#225): build and test commands that
  worked, flaky tests, common failure causes. Each one carries its evidence:
  the runs, by assignment id, and the result that showed it. Instructions,
  once approved.
- **Decisions:** design decisions an agent should respect, with a link to
  where they were made (an issue, a doc). Instructions.
- **History:** per run, the assignment, outcome, PR number, phase reached and
  a short scrubbed summary written by the node. Data.

**Who writes it:**

- The **operator** writes context and decisions directly.
- **Nodes** propose learnings, from measured results only, never because an
  agent said so, as #225 asks. A proposal goes to the control plane as data.
- **Agents** may suggest a learning in their checkpoint. The node forwards it
  as a proposal marked "agent suggestion", with the run it came from.
- Nothing proposed reaches a later run until the operator approves it (see
  "Open choices", question 4).

**How it reaches a run.** The assignment names a **knowledge snapshot** for
the repo: one signed bundle holding the context, approved learnings and
decisions, at a version. The node assembles the prompt as it does today, and
puts the snapshot in its own labelled section. Recent history for the issue
and repo (for example the last three runs on this issue) goes in a separate
fenced section marked as data. Both have a size cap, and the node, not the
control plane, decides where they sit in the prompt.

**How stale entries go:**

- A learning names what would contradict it (for example, "the test command
  is X": a later run where X fails for a reason other than the code). A node
  that measures a contradiction proposes dropping it, with its evidence.
- Learnings not confirmed by a run in 90 days are flagged for the operator,
  not dropped silently.
- Dropping or editing an approved entry is a new signed snapshot, so every
  run can say which version it used. The run's report carries the snapshot
  digest.

## Roles

A **role** is a signed bundle that says what a node is asked to be for one
piece of work:

```json
{
  "name": "builder",
  "version": 7,
  "pipelines": ["dev"],
  "instructions": "role.md",
  "skills": ["skills/tdd/SKILL.md", "skills/review/SKILL.md"],
  "agent": {"type": "claude", "model": "default", "max_turns": 200},
  "sandbox": {"extra_readonly": [], "extra_writable": [], "network": "default"},
  "credentials": ["sentry-read"],
  "repos": ["owner/app", "owner/lib"],
  "not_after": 1800000000
}
```

First roles, matching what the relay already does: **builder** (the dev
pipeline), **reviewer** (the review step), **triager** (#227), **planner**
(the plan step, #220). More are added by the operator, not by code.

**How a node takes one on.** The assignment from #251 gains two fields:

```
"role":      {"name": "builder", "digest": "sha256:4be1..."},
"knowledge": {"repo": "owner/app", "digest": "sha256:91c0..."}
```

The node fetches both by digest (`GET /v1/bundles/<digest>`), unless cached,
verifies them, and checks the role against the assignment: the pipeline and
repo must be in the role's lists. It writes the skills into a fresh
directory for this run only, outside `~/.claude/skills`, and points the agent
at it. The directory is removed when the run ends. Nothing a role brings
outlives its run.

**What a node still refuses.** Each node keeps a local `role_limits` block,
which the control plane cannot change:

```yaml
control_plane:
  operator_keys: ["ed25519:...", "ed25519:..."]
  role_limits:
    agents: [claude]                 # agent types a role may ask for
    max_extra_writable: []           # paths a role may add as writable; empty = none
    network: [default]               # sandbox network modes a role may ask for
    credentials: true                # whether this node accepts role credentials
```

A role that asks for more than these limits allow is refused
(`role_exceeds_limits`), even when signed. The signature says the operator
wrote it; the limits say what this machine allows. The sandbox from #219
still applies to every run.

## Starting clean

**What a node keeps locally, and why:**

| Kept on the node | Why |
|---|---|
| ctrlrelay, the agent CLI, bwrap | the runtime; the control plane sends no code |
| the agent login | it is an account, not a role credential (see question 3) |
| its GitHub token | the node reads issues and opens PRs with its own access (#251, rule 3) |
| node key, `node_id`, the `control_plane` block with `operator_keys` and `role_limits` | without them the node cannot reach the control plane, prove who it is, or check what it is sent |
| the state database, worktrees, the report queue | work in progress; claims on GitHub say who holds an issue |
| a bundle cache, by digest | speed only; safe to delete at any time |

**What it gets:** at register, the config (decision 6 of #251) and the
revocation list; at each assignment, the role and knowledge snapshot it
names, and any role credentials.

**What losing a node costs:** its runs in progress (their claims go stale and
another node takes over, as #251 describes) and any reports still queued on
disk. To keep that small, a node sends a learning proposal as soon as it has
one, not at the end of the run.

**The test for "starts clean":** take a node that has run an issue, delete
everything but the runtime, the agent login, the GitHub token and the local
`control_plane` block, register it again, and run the same issue. Its prompt
holds the same knowledge section, byte for byte, for the same snapshot
digest.

## Credentials

**Two classes, kept apart:**

- **Node credentials:** the node key, the GitHub token, the agent login.
  They never leave the node, as #251 rule 3 says today.
- **Role credentials:** secrets a role needs for its work, for example a
  read token for an error tracker, a package registry token, a scanner's API
  key. These live on the control plane.

**How one moves:**

1. The role names it (`"credentials": ["sentry-read"]`). A node takes only
   credentials named by the signed role it is running, and only if its
   `role_limits.credentials` is true.
2. At enrolment the node makes a second key pair, X25519, for receiving
   secrets. The control plane seals each credential to that one node's key,
   for that one assignment (`assignment_id` and an expiry inside the sealed
   box). Nothing in transit or in the control plane's logs is readable.
3. The node opens it, gives it only to the step that needs it, as an
   environment variable, and adds the exact value to the run's redaction
   list so it is scrubbed from every log, report and chat line.
4. It is never written into the prompt, the worktree or a file on disk, and
   it is dropped when the run ends, on release, and on revocation.

**Scope and life.** Prefer a token the control plane mints per assignment
(scoped to one repo, living as long as the lease) over a long-lived secret.
A long-lived secret is allowed, with its scope written in the role, and is
rotated at its source when a node holding it is revoked.

**What this does not cover:** the GitHub token and the agent login. Moving
those to the control plane is question 3.

## Threat model

Registered mode's threat model assumed a compromised control plane could
learn little and steer nothing. With the brain, the control plane holds the
factory's instructions and some of its secrets, so protecting it becomes the
main security job. Three attackers:

**1. Someone controls the control plane server, but not the operator key.**

| They can | They cannot, because |
|---|---|
| read all knowledge, history and roles | (nothing stops this; it is not secret from the server) |
| read role credentials it holds | (nothing stops this; keep them scoped and short-lived) |
| withhold work, route it badly, pause nodes | (as in #251) |
| insert false history | history is data, fenced, like issue text |
| | change a role, a skill, context or an approved learning: no operator signature |
| | hand an agent a new instruction: only signed content is shown as instructions |
| | roll a node back to an older, weaker role: nodes refuse lower versions |
| | widen what a role may do on a node: `role_limits` is local |
| | plant a learning: proposals need the operator's approval and signature |
| | give a node a credential its role does not name |

If config is not signed (question 2), this attacker can also change any
node's config, as decision 6 of #251 already accepts.

**2. Someone holds the operator key.** They control what every agent is told.
So the key lives off the server, ideally on a hardware key; there can be two
pinned keys so one can be rotated or revoked without touching every node by
hand; and every signature is logged with what it signed.

**3. Someone controls one node.** They can read the bundles and credentials
of the assignments that node gets, and all knowledge for the repos in its
allowlist. They can propose false learnings, which the operator then sees
with the node's name on them. Revoke the node's key, rotate the role
credentials it held, and the rest carry on.

**Protecting the control plane itself:** operator access behind SSO with a
second factor; an append-only audit log of every approval, signature, config
push and credential handed out; role credentials encrypted at rest; the
signing step on the operator's side, never a server job.

## Changes to registered mode, once agreed

- **Rule 3** becomes: "Node credentials (the node key, the GitHub token, the
  agent login) never leave the node. Role credentials may come from the
  control plane, sealed to one node for one assignment."
- **Assignments** gain `role` and `knowledge` digests (above). Without them
  a run behaves exactly as today.
- **Threat model:** the row "put its own instructions in front of the agent"
  changes to "unless signed with an operator key"; the row "get a credential"
  narrows to node credentials.
- **Non-goal** "the control plane holding or brokering GitHub or agent
  credentials" stays, unless question 3 changes it.

## Build order

1. **The protocol from #251, as agreed.** Reserve the `role` and
   `knowledge` fields in assignments and the bundle fetch path, so nothing
   here needs it reworked.
2. **Bundles and signing:** the manifest format, `ctrlrelay` commands for the
   operator to sign, node-side checks, the cache, the revocation list.
3. **Knowledge:** repo context and approved learnings as snapshots, the
   history section in the prompt, learning proposals from nodes (this is
   #225 moved to the control plane).
4. **Roles:** builder, reviewer, triager, planner as bundles, `role_limits`,
   per-run skill directories.
5. **Role credentials:** the second key, sealed delivery, redaction.

## Open choices

For the owner to decide.

1. **Signing.** Proposed: everything that steers an agent is signed by an
   operator key held off the server. Other option: trust the control plane
   and rely on access control and the audit log; simpler, but a hacked
   server steers every node.
2. **Is config signed too?** Proposed: yes, so a hacked server cannot change
   repos, sandbox paths or schedules either. This adds a signature to
   decision 6 of #251. Other option: keep config unsigned, as decided.
3. **GitHub token and agent login.** Proposed: they stay on the node for now,
   so a node is "clean" of knowledge but not of its own accounts. Other
   option: the control plane hands those out too, for a fully blank node,
   at the price of the server holding every account.
4. **Approving learnings.** Proposed: every learning, measured or suggested,
   needs the operator's approval before a run sees it. Other option: facts a
   node measured are used at once and only agent suggestions wait; faster,
   but a hacked server could plant a "fact" such as a test command.
