---
title: Claude Code project guide
layout: default
parent: Design & history
nav_order: 2
permalink: /reference/claude-code-project-guide/
---

# Claude Code Project Development Guide

## Core Frame

You are directing a very fast, capable engineer. You own the problem, the scope, the tradeoffs, and the quality bar. The AI executes. You decide.

This guide governs how you approach project creation, tool building, and feature implementation with Claude Code and superpowers skills.

---

## Part 1: The Five Dimensions of Effective AI-Assisted Development

These dimensions determine success — in order of importance:

### 1. Problem Decomposition Before Touching the Tool

Restate the problem clearly, identify the most important unknown, and sequence your approach before writing a single prompt. This is the single highest-signal behavior in the first five minutes.

### 2. Prompt Precision and Iteration Discipline

Give scoped, specific prompts — not vague ones. Iterate deliberately, one concern at a time. Do not try to get everything in one shot and then patch the output.

### 3. Critical Review of AI Output

Read what the AI produced before running it. State what you are checking and why. Catch when the output does not match the requirement — do not just run it and hope.

### 4. Explicit Tradeoff Decisions

Make scope, architecture, and "good enough" decisions explicitly and out loud. Do not silently pick the first approach suggested. Name the tradeoff before making it.

### 5. Collaborative Posture

Treat users and reviewers as peers whose input you want — not an audience you are performing for. Ask "does this approach match what you had in mind?" when appropriate.

---

## Part 2: The First 5 Minutes — Before Any Implementation

**STOP. Do these four things before writing any code:**

| Step | Action | Example |
|:----:|:-------|:--------|
| 1 | **Restate the problem** in your own words | "So what I am hearing is we need to build X that does Y for Z user. Let me make sure I understand the scope before I start." |
| 2 | **Ask one clarifying question** — the most important unknown | Not five questions. One. The one thing that would most change your approach if the answer were different. |
| 3 | **State what you are NOT building** | "I am going to scope this to the core workflow and not try to handle every edge case — the goal is a working tool, not a production system." |
| 4 | **Name your approach** before prompting | "I am going to scaffold the data model first, then wire the output, then handle edge cases. Does that sequencing make sense?" |

**Watch out:** Most developers open the AI tool immediately. The 2 minutes spent on steps 1-4 are the highest-value 2 minutes of any project. Do not skip them under pressure.

---

## Part 3: The Spec — Four Questions Before Any Prompt

Before writing any prompt, answer these four questions. They become your prompt foundation AND your acceptance test:

### Q1: What is the INPUT?
A CSV file, a text prompt, user typing something, a list of items — be specific.

### Q2: What is the OUTPUT?
A CLI result, a report file, a formatted summary, a prioritized list — be specific.

### Q3: Who USES it?
You, a program manager, an engineering director — this determines what "readable" means.

### Q4: What is the ONE THING it must do correctly?
If it only does one thing right, what is that thing? This is your acceptance test.

### Example Spec

```
INPUT:  A CSV with columns: finding_name, severity, owner, status
OUTPUT: A prioritized remediation list sorted by severity,
        with a count summary at the end
USER:   A program manager reviewing compliance posture
MUST:   Critical findings appear at the top, unassigned ones flagged clearly
```

Save as `spec.md` — this is your prompt foundation and acceptance test.

---

## Part 4: Skill Invocation Requirements

**MANDATORY**: Check for applicable skills BEFORE any implementation.

### Required Skills by Task Type

| Task | Required Skill | Invoke BEFORE |
|:-----|:---------------|:--------------|
| Building anything new | `brainstorming` | Writing any code |
| Multi-step implementation | `writing-plans` | Starting work |
| Any feature or bugfix | `test-driven-development` | Writing implementation |
| Bug or test failure | `systematic-debugging` | Proposing fixes |
| Risk assessment needed | `vid` | Reviewing risky changes |
| Claiming work is done | `verification-before-completion` | Committing or PRs |

### Skill Invocation Flow

```
User request received
    ↓
STOP — check for applicable skills
    ↓
Invoke skill(s) via Skill tool
    ↓
Follow skill instructions exactly
    ↓
Continue with implementation
```

### Red Flags — Stop If You Think These

| Thought | Reality |
|:--------|:--------|
| "This is simple" | Simple becomes complex. Use skills. |
| "Let me explore first" | Skills tell you HOW to explore. Check first. |
| "I'll just do this one thing" | Check skills BEFORE any action. |
| "The skill is overkill" | Discipline saves time. Use it. |
| "I know what to do" | Skills evolve. Read current version. |

---

## Part 5: Verified Intent Development (VID)

VID governs ALL code generation. Apply these principles implicitly, not just when asked.

### Core Principles

1. **Intent Before Generation**: Never write code without understanding what should be built and how correctness will be verified.

2. **Graduated Trust**: Match verification depth to risk level. Not all code deserves equal scrutiny.

3. **Understanding Over Acceptance**: Never produce code the user cannot understand. Prefer clear over clever.

4. **Provenance Awareness**: Track where code comes from. Flag AI-generated patterns and confidence levels.

5. **Continuous Calibration**: Learn from outcomes. Adjust verification if it missed bugs or was excessive.

### Risk Scoring

Score every non-trivial code change:

```
Risk Score = (Impact × 3) + (Reversibility × 2) + (Exposure × 2) + Compliance
```

| Dimension | 1 (Low) | 3 (Medium) | 5 (High) |
|:----------|:--------|:-----------|:---------|
| **Impact** | Cosmetic issue | User-facing bug, no data loss | Data corruption, security hole, financial impact |
| **Reversibility** | Instant (redeploy) | Moderate (DB rollback) | Impossible (sent emails, data loss) |
| **Exposure** | Developer only | Company internal | Public / all users |
| **Compliance** | None (0) | GDPR, accessibility (2-4) | HIPAA, PCI, life-safety (6-10) |

### Trust Levels and Verification Depth

| Score | Trust Level | Verification Time | Actions Required |
|:------|:------------|:------------------|:-----------------|
| 0-10 | High | 5-10 min | Read code, verify intent, run automated checks |
| 11-20 | Moderate | 15-30 min | + edge case testing, basic security review |
| 21-30 | Guarded | 30-60 min | + adversarial testing, security audit, peer review |
| 31-47 | Minimal | 1-3+ hours | + formal verification, multiple reviewers, sign-offs |

**Escalation Rule**: If ANY dimension scores >= 4, OR Compliance >= 6, move to next trust level regardless of total score.

### When to Flag Risk

| Scenario | Action |
|:---------|:-------|
| Trivial change (config, typo, formatting) | Just do it |
| Standard feature work | Mentally assess, flag if Moderate+ |
| Touches auth, payments, data deletion, external APIs | ALWAYS flag |

---

## Part 6: The Prompt Process — From Spec to Working Tool

**Four phases — do not skip phases, do not combine them.**

### Phase 1: The Scaffold Prompt

Get something that runs first. Happy path only. No edge cases. No polish.

```markdown
I need to build a [tool type] that does the following:

[paste your four-question spec]

Start by creating the simplest working version that handles
the happy path only. Use Python. Single file. No external
dependencies beyond the standard library unless absolutely necessary.
```

### Phase 2: Iterate in Layers (One Concern per Prompt)

After the scaffold runs, add one layer at a time. Each prompt is one concern:

- "Now add error handling for missing or malformed input"
- "Now make the output readable for a non-technical program manager"
- "Now add a summary count at the end: X critical, Y high, Z medium"
- "Now flag any findings where owner is blank or missing"

**Watch out:** Do not try to get everything in one prompt. One concern at a time keeps the output reviewable and prevents the AI from going off in a direction you cannot evaluate.

### Phase 3: Review Before Running (Every Time)

After every AI output, state what you are checking before you run it:

- "Let me check how it handled the edge case where severity is missing before I accept this"
- "I want to verify the sort order is correct — critical first, then high, then medium"
- "This looks right for the happy path — let me run it against a test input that has a blank owner"

**The key move:** Never silently accept AI output. Reading it critically is what shows you govern the tool, not the other way around.

### Phase 4: The Single Final Prompt

Once the tool works, distill everything learned into one prompt that produces the complete working version from scratch:

```markdown
You are building a [tool name] for [user] that [core purpose].

Requirements:
- [requirement 1]
- [requirement 2]
- [requirement 3]

Constraints:
- Single Python file
- No external dependencies
- Output must be human-readable for a non-technical program manager
- Handle these edge cases: [list them]

Produce the complete working implementation.
```

Test by pasting into a clean session with no prior context. If it produces the working tool on the first run — that is your single prompt.

---

## Part 7: When the AI Gets It Wrong

When the AI produces something incorrect, do not silently patch it. State this:

> "This is not quite right — it assumed [X] but the requirement is [Y]. Let me correct the prompt rather than patch the output, because patching AI output creates drift from the original intent."

Then re-prompt with the correction. This sequence shows three things:
- You understood the requirement well enough to spot the mismatch
- You know that fixing output instead of fixing the prompt is a bad practice
- You can self-correct without losing momentum

### Common AI Failures — How to Catch Them

| What the AI Does | How to Catch It | What to Say |
|:-----------------|:----------------|:------------|
| Over-engineers the solution | Check if output matches spec scope, not just whether it runs | "This is more than I need — let me scope it back to the core requirement" |
| Assumes an input format you did not specify | Test with a real or realistic input before declaring done | "I need to validate this against an actual input before I move on" |
| Uses an external library that may not be installed | Check import statements in the first 5 lines | "I want to keep this dependency-free — let me re-prompt to use the standard library" |
| Produces output formatted for a developer, not the user | Read the output as if you are the user — would they understand it? | "The output is correct but not readable for the audience — let me fix the formatting" |
| Handles the happy path but silently fails on edge cases | Always test one bad input after the happy path works | "Let me run this with a missing severity field before I call it done" |

---

## Part 8: Phrases That Signal You Are Governing the Tool

Use these phrases — they demonstrate you are directing the AI, not following it:

| Say This | Why It Works |
|:---------|:-------------|
| "The user of this tool is a program manager, not an engineer — so the output needs to be readable, not just correct" | Shows you think about audience and purpose, not just technical correctness |
| "I am going to validate this against a real scenario before I add more features" | Shows you test before extending — quality discipline |
| "This is good enough for the problem as stated — if the scope were larger I would handle X differently" | Shows explicit scope judgment — knowing when done is done |
| "Let me check whether the AI handled the edge case where the input is missing — that is the most likely real-world failure" | Shows you think about failure modes, not just success paths |
| "I am correcting the prompt rather than patching the output — patching creates drift" | Shows you understand AI governance, not just AI use |
| "Does this approach match what you had in mind, or would you pull this differently?" | Treats the user as a peer whose input matters |
| "I am keeping this prompt narrow so I can validate the data model before I add the interface" | Shows deliberate iteration discipline — one concern at a time |
| "I could use a database here but for the scope of this problem a flat file is faster and sufficient — I am making that call deliberately" | Explicit tradeoff decision — this is what senior-level sounds like |

---

## Part 9: Model Selection & Claude Code Commands

### Model Strings (as of March 2026)

| Model | API String | Use Case |
|:------|:-----------|:---------|
| **Claude Opus 4.6** (default) | `claude-opus-4-6` | Complex reasoning, architecture, difficult problems |
| **Claude Opus 4.5** | `claude-opus-4-5-20251101` | Previous Opus — use explicitly if needed |
| **Claude Sonnet 4.6** | `claude-sonnet-4-6` | Fast iteration, standard development |
| **Claude Haiku 4.5** | `claude-haiku-4-5-20251001` | Rapid scaffolding, simple tasks |

### CLI Commands

```bash
# Use default (Opus 4.6)
claude "your prompt here"

# Use Opus 4.5 explicitly
claude --model claude-opus-4-5-20251101 "your prompt here"

# Run with a spec file as input
claude --model claude-opus-4-5-20251101 < spec.md

# Pipe output to a file
claude "your prompt" > output.py
```

### Python API Usage

```python
import anthropic

client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var

response = client.messages.create(
    model="claude-opus-4-5-20251101",
    max_tokens=4096,
    messages=[
        {"role": "user", "content": open("spec.md").read()}
    ]
)

print(response.content[0].text)
```

### MCP Tools Available

Use these when applicable:
- **Browser automation**: Playwright, Chrome DevTools
- **Code review**: Codex reviewer tools
- **Documentation**: Context7 for current library docs

---

## Part 10: VID Verification Checklists

### High Trust (0-10 points)
- [ ] Read code completely
- [ ] Verify it matches stated intent
- [ ] Run automated checks (lint, types, tests)
- [ ] 30-second "what could go wrong?" reflection

### Moderate Trust (11-20 points)
- [ ] All High Trust items
- [ ] Trace logic through main paths
- [ ] Test edge cases (nulls, empty, boundaries)
- [ ] Check integration with existing code
- [ ] Basic security review (inputs validated? auth checked?)

### Guarded Trust (21-30 points)
- [ ] All Moderate Trust items
- [ ] Adversarial thinking — actively try to break it
- [ ] Security audit (STRIDE: Spoofing, Tampering, Repudiation, Info Disclosure, DoS, Elevation)
- [ ] Performance review (complexity, resources, connections)
- [ ] Recommend peer review

### Minimal Trust (31-47 points)
- [ ] All Guarded Trust items
- [ ] Require 2+ independent reviewers
- [ ] Formal security review with tools
- [ ] Comprehensive documentation
- [ ] Rollback plan documented
- [ ] Tech lead sign-off

---

## Part 11: Test Verification

Tests need verification too — do not trust AI-generated tests blindly:

1. **Does each test actually test what it claims?** Read the assertion, not the name
2. **Are assertions correct?** Wrong expected values = false confidence
3. **What's NOT tested?** Edge cases, error paths, security scenarios
4. **Behavior vs implementation?** Tests should verify outcomes, not internals
5. **Mutation check**: Would test catch a bug if implementation changed?

---

## Part 12: Commit Conventions for AI-Assisted Code

Include provenance context:

```
feat(auth): add JWT token refresh logic

AI-assisted: token rotation logic generated with Claude, verified
against OWASP session management guidelines.

Risk: Moderate (Impact:3, Reversibility:2, Exposure:4, Compliance:2 = 19)
Verification: edge case testing, security review of token handling
```

---

## Part 13: The Full Process — Summary

### Step 1 — Write the Spec (Before Any AI Tool)
- What is the input?
- What is the output?
- Who uses it?
- What is the one thing it must do correctly?

*Save as spec.md — this is your prompt foundation and acceptance test.*

### Step 2 — Invoke Required Skills
- `brainstorming` for requirements exploration
- `writing-plans` for multi-step work
- `test-driven-development` if appropriate
- Assess VID risk level

### Step 3 — Scaffold Prompt (Happy Path Only)
- Simplest working version
- Single file, no external dependencies
- Do not ask for edge cases, error handling, or polish yet

### Step 4 — Iterate in Layers (One Concern per Prompt)
- Error handling for bad input
- Output formatting for the actual user
- Edge cases identified in the spec
- Summary or reporting layer

### Step 5 — Review Before Running Every Time
- Read the output critically — state what you are checking
- Test with a realistic input, not just a perfect one
- Fix the prompt, not the output, when something is wrong
- Flag if VID risk level changes

### Step 6 — Verify Before Completion
- Run appropriate VID verification checklist
- Invoke `verification-before-completion` skill
- Run tests / verification commands
- Confirm output matches requirements
- Test in clean environment if applicable

### Step 7 — Harden into a Single Final Prompt
- Distill requirements + constraints + edge cases into one prompt
- Test in a clean session with no prior context
- If it produces the working tool on the first run — done

---

## What NOT to Do

- **Do not open the AI tool before doing the spec and sequencing steps**
- **Do not silently accept AI output** — always read and verify
- **Do not over-build** — scope discipline is a senior signal
- **Do not let the AI drive** — you drive, the AI executes
- **Do not patch output** — fix the prompt instead
- **Do not skip skills** — they encode hard-won best practices
- **Do not claim completion without evidence** — verify first

---

## The One Sentence

**You own the problem, the scope, and the quality bar. The AI executes.**
