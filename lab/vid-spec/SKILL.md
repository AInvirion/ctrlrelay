---
name: vid-spec
description: "Create project specifications using the Verified Intent Development (VID) methodology. Use this skill whenever the user wants to: create a project spec, write a technical specification, define requirements for a new project, plan a software project, write an intent specification, assess project risk, or create a verification plan. Also trigger on: 'spec', 'project plan', 'requirements document', 'technical requirements', 'project scope', 'new project', 'VID', 'intent specification', 'risk assessment for project', even if the user doesn't explicitly mention VID. If someone is starting a new software project and needs to define what to build, this is the right skill."
---

# VID Project Specification Skill

Generate comprehensive project specifications grounded in the Verified Intent Development methodology. VID's core insight: in AI-augmented development, **verification is the bottleneck, not generation**. Every spec produced by this skill embeds verification thinking from the start.

## Before You Start

Read the appropriate reference file based on what the user needs:

- **For any spec creation**: Read `references/vid-core.md` — contains the full methodology, risk scoring, verification checklists, and decision trees.
- **For templates and formatting**: Read `references/vid-templates.md` — contains all intent specification templates (Quick, Extended, Formal), verification ritual checklists by trust level, risk classification worksheets, and provenance annotation guides.
- **For real-world context**: Read `references/vid-examples.md` — contains five worked examples showing VID catching real bugs, plus patterns and anti-patterns.

Always read `vid-core.md` first. Read additional references as needed for the specific task.

## Workflow

### Phase 1: Gather Project Context

Ask the user about:

1. **What** they want to build (one sentence, then expand)
2. **Why** it needs to exist (business/technical justification)
3. **Who** will use it (audience, scale, exposure)
4. **What exists now** (greenfield vs. extending existing system)
5. **Constraints** (timeline, tech stack, compliance requirements, team size)
6. **What "done" looks like** (success criteria they care about)

Do not proceed to spec writing until you have clear answers. Ambiguity here propagates into every downstream decision.

### Phase 2: Decompose Into Components

Break the project into discrete functional components. For each component, identify:

- Core responsibility (one sentence)
- Inputs and outputs
- Integration points with other components
- External dependencies

### Phase 3: Risk Assessment Per Component

Apply the VID risk scoring formula to each component:

```
Risk Score = (Impact x 3) + (Reversibility x 2) + (Exposure x 2) + Compliance
```

**Dimensions:**

| Dimension | Scale | Weight | Question |
|-----------|-------|--------|----------|
| Impact | 1-5 | x3 | What happens if this component has a bug? |
| Reversibility | 1-5 | x2 | How hard is it to fix or roll back? |
| Exposure | 1-5 | x2 | Who is affected? |
| Compliance | 0-10 | x1 | What regulatory requirements apply? |

**Trust Level Mapping:**

| Score | Trust Level | Verification Time | Implication for Spec |
|-------|-------------|-------------------|---------------------|
| 0-10 | High Trust | 5-10 min | Lightweight spec, basic tests |
| 11-20 | Moderate Trust | 15-30 min | Standard spec, edge case coverage |
| 21-30 | Guarded Trust | 30-60 min | Detailed spec, security review, peer review |
| 31-47 | Minimal Trust | 1-3+ hours | Formal spec, multiple reviewers, compliance docs |

**Escalation rules** (override total score):
- Any dimension >= 4: escalate to next trust level
- Compliance >= 6: escalate to next trust level
- Impact = 5: immediately Minimal Trust

### Phase 4: Write the Spec

Use the appropriate template depth based on the highest risk component:

**For High Trust projects** (score 0-10): Use the Quick Intent Template from `references/vid-templates.md`. One page is enough.

**For Moderate Trust projects** (score 11-20): Use the Extended Intent Template. Include edge cases, non-functional requirements, and a verification plan.

**For Guarded/Minimal Trust projects** (score 21+): Use the Formal Specification template. Every component needs its own detailed spec section.

### Spec Document Structure

Produce a markdown document with this structure:

```markdown
# Project Specification: [Project Name]

## Meta
- **Author:** [user name]
- **Date:** [today]
- **Overall Risk Level:** [Trust Level] (Highest component score: X/47)
- **Methodology:** Verified Intent Development (VID)

## 1. Project Overview
### Purpose
[Why this project exists — the problem it solves]

### Current State
[What exists now, what's broken or missing]

### Desired State
[What should exist after implementation]

### Success Criteria
[Observable, measurable outcomes that define "done"]

## 2. Architecture Overview
[High-level component diagram or description]
[How components interact]
[Key technical decisions and their rationale]

## 3. Component Specifications

### Component: [Name]
**Risk Score:** X/47 — [Trust Level]

| Dimension | Score | Rationale |
|-----------|-------|-----------|
| Impact | X/5 | [Why] |
| Reversibility | X/5 | [Why] |
| Exposure | X/5 | [Why] |
| Compliance | X/10 | [Why] |

**Functional Requirements:**
- [ ] [Requirement with measurable outcome]

**Edge Cases:**
- [Edge case]: [Expected behavior]

**Non-Functional Requirements:**
- Performance: [Specific targets]
- Security: [Specific requirements]

**Verification Plan:**
- [ ] [Verification step appropriate to trust level]

**Dependencies:**
- [What this component depends on]
- [What depends on this component]

[Repeat for each component]

## 4. Integration Map
[How components connect]
[API contracts between components]
[Data flow]

## 5. Non-Functional Requirements (Project-Wide)
### Performance
### Security
### Scalability
### Compliance

## 6. Verification Strategy
[Overall verification approach]
[Which components need peer review]
[Which need security specialist review]
[Test strategy per trust level]

## 7. Provenance Plan
[How to track AI-generated vs human-written code]
[Commit message conventions]
[Code annotation standards]

## 8. Implementation Order
[Recommended build sequence]
[Dependencies between components]
[What can be parallelized]

## 9. Risk Register
[Top risks and mitigations]
[What could go wrong at the project level]

## 10. Assumptions and Constraints
[What we're assuming to be true]
[Hard constraints that limit options]
```

### Phase 5: Review and Refine

After generating the spec, self-check:

1. **Completeness**: Every component has a risk score, requirements, edge cases, and verification plan.
2. **Consistency**: Risk scores align with the verification plans. High-risk components have proportionally deeper verification.
3. **Testability**: Every requirement is written so someone could verify it passed or failed.
4. **No hand-waving**: No "TBD" or "to be determined" in critical sections. If something is unknown, say so explicitly and flag it as a risk.
5. **Provenance plan exists**: The spec includes how to track code origins throughout the project.

## Output Format

Always save the spec as `VID-SPEC.md` in the workspace. Use this exact filename every time — no variations, no project-name prefixes. For Guarded Trust and above, also offer to produce a `.docx` version (named `VID-SPEC.docx`) using the docx skill.

## Key Principles to Embody

These five VID principles should permeate every spec you write:

1. **Intent Before Generation** — The spec IS the intent. It must be clear enough that any developer (human or AI) can generate correct code from it.
2. **Graduated Trust** — Not every component deserves the same scrutiny. Score risk, then allocate verification proportionally.
3. **Understanding Over Acceptance** — The spec should explain WHY, not just WHAT. A developer reading this spec should understand the reasoning behind every decision.
4. **Provenance Awareness** — Build tracking into the project from day one. It costs nothing now and saves enormously later.
5. **Continuous Calibration** — The spec is a living document. Include a section on how to update it as understanding grows.

## Anti-Patterns to Avoid

- Writing specs that are purely aspirational with no verification plan
- Uniform risk treatment (everything gets the same level of detail)
- Missing edge cases in "simple" components (the delete endpoint problem)
- Specs that describe WHAT but not WHY
- No provenance plan ("we'll figure that out later")
- Ignoring compliance dimensions in risk scoring
