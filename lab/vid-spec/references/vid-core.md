# VID Core Methodology Reference

> Verified Intent Development (VID) — A methodology for software development in the age of AI code generation.
> Created by Oscar Valenzuela (SEMCL.ONE Community)
> License: CC BY-SA 4.0
> Source: https://github.com/SemClone/Verified-Intent-Development

## Table of Contents

1. [The Core Problem](#the-core-problem)
2. [The Five Principles](#the-five-principles)
3. [Risk Scoring](#risk-scoring)
4. [Trust Levels and Verification](#trust-levels-and-verification)
5. [The Four Core Practices](#the-four-core-practices)
6. [Decision Trees](#decision-trees)
7. [Verification Toolkit](#verification-toolkit)
8. [Patterns and Anti-Patterns](#patterns-and-anti-patterns)
9. [For AI Assistants](#for-ai-assistants)

---

## The Core Problem

The bottleneck of software development has inverted. AI generates production-grade code in seconds, but cannot guarantee correctness, security, or maintainability. Every methodology before VID (Waterfall, Agile, Scrum, XP) optimized for reducing coding effort. That assumption collapsed. The problems moved from generation to verification.

VID builds the judgment, practices, and habits that allow developers to move fast with confidence. Value now comes from deciding what to build, how to validate it, and when to accept or reject generated artifacts.

---

## The Five Principles

### 1. Intent Before Generation

Never generate code without first articulating what you intend to build and how you will verify correctness.

Before generating, answer:
- **Functional intent**: What should this code do? Inputs, outputs, transformation?
- **Quality intent**: Performance requirements? Reliability? Acceptable trade-offs?
- **Boundary intent**: Valid inputs? Invalid input behavior? Edge cases?
- **Integration intent**: How does this interact with existing code? What contracts must it honor?

Write verification criteria before generating:
- Specific test cases with expected outputs
- Properties that must hold (e.g., "output is always sorted")
- Security requirements (e.g., "rejects SQL injection attempts")
- Performance requirements (e.g., "responds in under 100ms for 1000 items")

If you cannot articulate these, you are not ready to generate.

### 2. Graduated Trust

The level of verification should match the level of risk, not be uniform.

Risk is scored across four dimensions (see Risk Scoring below). The result maps to a trust level that determines verification depth:

- **High Trust** (score 0-10): Automated checks, brief review
- **Moderate Trust** (score 11-20): Systematic edge case testing, basic security review
- **Guarded Trust** (score 21-30): Adversarial testing, security audit, peer review
- **Minimal Trust** (score 31-47): Formal verification, multiple reviewers, sign-offs

### 3. Understanding Over Acceptance

Never accept code you do not understand at the depth its risk demands.

Levels of understanding:
- **Surface**: "This takes X and returns Y. I can use it correctly." — For low-risk utilities.
- **Functional**: "I understand the algorithm and why it produces correct results." — For most production code.
- **Deep**: "I understand implementation details, edge cases, performance, and security implications." — For high-risk code.

Code that works but nobody understands is a liability that compounds over time.

### 4. Provenance Awareness

Always know where code came from and what that origin implies.

Provenance categories:
- **Human original**: Lowest inherent risk
- **AI generated, human verified**: Risk depends on verification depth
- **AI generated, lightly reviewed**: Higher risk
- **AI generated, unreviewed**: Highest risk
- **Mixed provenance**: Risk depends on integration verification

Track provenance for: incident investigation, modification planning, risk assessment, IP/license compliance.

### 5. Continuous Calibration

Regularly assess whether your verification practices match actual risk and adjust accordingly.

Ask regularly:
- Are we catching problems? (If rarely rejecting: too lenient)
- Are we missing problems? (If issues escape to production: strengthen verification)
- Is verification effort appropriate? (If misallocated: adjust risk calibration)
- Are our risk assessments accurate? (If "low-risk" code causes incidents: recalibrate)

---

## Risk Scoring

### Formula

```
Risk Score = (Impact x 3) + (Reversibility x 2) + (Exposure x 2) + Compliance
```

Range: 0 (minimal) to 47 (maximum)

### Dimension 1: Impact (1-5) — Worst-case if code has a bug

| Score | Severity | Examples |
|-------|----------|---------|
| 1 | Trivial | Log typo, comment formatting, debug output |
| 2 | Minor | UI alignment, suboptimal sort, redundant call |
| 3 | Moderate | Feature does not work, incorrect non-critical calc |
| 4 | Serious | Payment error, data loss, auth bypass, PII exposure |
| 5 | Critical | Complete outage, fraud, regulatory violation, safety failure |

### Dimension 2: Reversibility (1-5) — Recovery difficulty

| Score | Recovery | Examples |
|-------|----------|---------|
| 1 | Instant | Stateless API, display logic, formatting |
| 2 | Easy | Cache invalidation, restart, temp config change |
| 3 | Moderate | DB migration rollback, batch rerun |
| 4 | Hard | Manual data correction, multi-system coordination |
| 5 | Impossible | Deleted production data, wrong payments sent |

### Dimension 3: Exposure (1-5) — Who is affected

| Score | Reach | Examples |
|-------|-------|---------|
| 1 | Developer only | Local dev tool, personal script |
| 2 | Team internal | Team dashboard, internal CI |
| 3 | Company internal | Employee portal, internal API |
| 4 | External limited | Beta features, specific customer segment |
| 5 | Public/Universal | Public website, main API, critical service |

### Dimension 4: Compliance (0-10) — Regulatory requirements

| Score | Burden | Examples |
|-------|--------|---------|
| 0 | None | Internal tooling, general business logic |
| 2 | Basic | Standard web security, data retention |
| 4 | Moderate | GDPR, accessibility, SOC2 |
| 6 | Significant | HIPAA, PCI-DSS, SOX |
| 8 | Critical | FDA, financial trading regulations |
| 10 | Existential | Critical infrastructure, safety systems |

### Trust Level Mapping

| Risk Score | Trust Level | Verification Time |
|------------|-------------|-------------------|
| 0-10 | High Trust | 5-10 min |
| 11-20 | Moderate Trust | 15-30 min |
| 21-30 | Guarded Trust | 30-60 min |
| 31-47 | Minimal Trust | 1-3 hours + peer review |

### Escalation Rules (override total score)

- Any dimension (Impact, Reversibility, Exposure) >= 4: Move to next trust level
- Compliance >= 6: Move to next trust level
- Impact = 5: Defaults immediately to Minimal Trust
- When in doubt: score conservatively (round up)

### Worked Examples

**Log message formatter**: I=1, R=1, E=2, C=0 -> Score 9 -> High Trust
**User profile endpoint**: I=3, R=3, E=4, C=4 -> Score 27 -> Guarded Trust
**Payment processing**: I=5, R=4, E=4, C=6 -> Score 37 -> Minimal Trust

---

## Trust Levels and Verification

### High Trust (Score 0-10) — 5-10 minutes

- Read entire code (not skimmed)
- Verify it matches stated intent
- Run automated checks (lint, types, tests)
- 30-second "what could go wrong?" reflection
- Spot-check one edge case

### Moderate Trust (Score 11-20) — 15-30 minutes

All High Trust items, plus:
- Trace logic mentally through main paths
- Identify 4-5+ input categories, test one from each
- Test boundary values (min, max, boundary +/- 1)
- Test error conditions
- Check integration with existing code
- Basic security review (inputs validated? auth checked?)
- Error messages do not leak sensitive info
- Document edge cases and assumptions

### Guarded Trust (Score 21-30) — 30-60 minutes

All Moderate Trust items, plus:
- Adversarial thinking: actively try to break it
- STRIDE threat analysis (Spoofing, Tampering, Repudiation, Info Disclosure, DoS, Elevation)
- Test with malicious inputs, extreme values, concurrent access
- Performance review (O(n) complexity, resource usage)
- Maintainability review (stranger test, naming audit)
- Peer review by another developer

### Minimal Trust (Score 31-47) — 1-3+ hours

All Guarded Trust items, plus:
- 2+ independent reviewers
- Security specialist review
- Formal threat modeling
- Mutation testing (target >90% kill rate)
- Comprehensive documentation (architecture, security model, failure modes)
- Tech lead sign-off
- Rollback plan documented

---

## The Four Core Practices

### Practice 1: Intent Specification

Capture requirements, boundaries, and success criteria before generating code. Match formality to risk:
- Trivial: Mental intent is sufficient
- Typical: Comment or test-first
- Important: Test-first with edge cases
- Critical: Formal specification with comprehensive tests

Anti-pattern: Retroactive Intent — writing intent after generation defeats the purpose. You will rationalize that the output matches your intent rather than critically evaluating it.

### Practice 2: Verification Rituals

Apply consistent verification after every generation, scaled to the trust level. The key discipline: make verification automatic — no decision-making about whether to do it. The trust level determines the ritual; you execute it.

### Practice 3: Learning Loop

Track outcomes and adjust practices:
- When verification catches a problem: the system works
- When a problem escapes to production: verification was insufficient — strengthen it
- When verification feels excessive: you may be over-verifying — adjust downward
- Weekly retrospective: What escaped? What felt excessive? Adjust.

### Practice 4: Provenance Hygiene

Document and maintain awareness of code origins.

Commit message format:
```
[VID] <type>: <description>

<body>

Provenance: <AI-generated | Human-written | AI-assisted | Migrated>
Verification: <Trust Level>
Verified-by: <username>
Risk-score: <0-47>
```

Code annotations for significant AI-generated code:
```
# @provenance: AI-generated (Claude)
# @verified-by: engineer_name
# @risk-score: 14 (Moderate Trust)
```

---

## Decision Trees

### Should I Use AI for This Task?

1. Is this security-critical code (auth, crypto, payments, PII)? -> Use AI for reference only, write yourself, apply Minimal Trust, mandatory peer review.
2. Do I have a clear intent specification? -> YES: safe to use AI. NO: write intent spec first.

### What Trust Level?

1. Calculate Risk Score: (Impact x 3) + (Reversibility x 2) + (Exposure x 2) + Compliance
2. Map to trust level (0-10 High, 11-20 Moderate, 21-30 Guarded, 31-47 Minimal)
3. Escalation check: any dimension >= 4? Compliance >= 6? -> Escalate one level

### Should I Accept This Code?

Five gates, all must pass:
1. Do I understand what this code does? (at appropriate depth)
2. Does it match my intent specification?
3. Did verification pass at the appropriate trust level?
4. Is verification depth appropriate for the risk?
5. Can I maintain this code in 6 months?

All YES -> Accept. Any NO -> Reject or fix.

### When Production Breaks

1. Is this AI-generated code? Check provenance.
2. What verification was performed? Check commit/PR notes.
3. Should verification have caught this? -> Yes: update practices. No: add regression test.
4. Can the original developer fix it? -> No: understanding debt problem.

---

## Verification Toolkit

### Functional Verification

**Input Space Partitioning** — Divide inputs into categories, test at least one from each:
- Numeric: negative, zero, positive, very large, very small, NaN, Infinity
- Strings: empty, single char, typical, very long, unicode, special chars, null
- Collections: empty, single element, multiple, very large, duplicates, nulls
- Dates: normal, leap year Feb 29, month boundaries, year boundaries, timezone/DST

**Boundary Value Analysis** — Test at boundary, boundary-1, boundary+1:
- Array indices: 0, length-1, length
- Numeric ranges: min, min+1, max-1, max
- String lengths: 0, 1, max-1, max

**Property-Based Reasoning** — Test invariants:
- Round-trip: decode(encode(x)) == x
- Idempotency: f(f(x)) == f(x)
- Symmetry: reverse(reverse(x)) == x
- Ordering: output is always sorted
- Bounds: output within expected range

### Security Verification

**Input Vector Enumeration** — List every input, ask "what if from attacker?":
URL params, form fields, headers, cookies, file uploads, DB contents, env vars, external APIs

**Injection Point Analysis** — Find every place input combines with commands/queries:
- SQL: use parameterized queries (never string concatenation)
- Shell: avoid if possible; whitelist if necessary
- HTML: escape all output
- File paths: validate against whitelist

**Auth/Authz Audit** — For every action:
1. Does it require authentication? Should it?
2. Does it verify authorization? Should it?
3. Can auth/authz be bypassed?

**Data Exposure Analysis** — Trace sensitive data through code:
- Sensitive: passwords, API keys, PII, financial data, session tokens
- Leaks via: logs, error messages, API responses, URLs, client storage, source code

### Maintainability Verification

**The Stranger Test** — Pretend you have never seen the code:
- <5 min to understand: Good
- 5-15 min: Acceptable for complex code
- 15+ min for simple code: Problem

**Complexity Check:**
- Cyclomatic complexity >10: scrutinize
- Nesting >3 levels: simplify
- Function >30 lines: break up
- Parameters >4: reconsider

---

## Patterns and Anti-Patterns

### Patterns (Do This)

- **Specification-First**: Write spec with ALL edge cases before generation
- **Graduated Verification**: Different risk = different investment
- **Understanding Before Acceptance**: If you cannot debug it when it breaks, you do not understand it
- **Provenance Tracking From Day One**: Mark AI-generated code in commits and comments
- **Security Review = Threat Modeling**: Do not just check if code "looks right"
- **Maintainability Investment**: One hour refactoring AI-generated code saves months later

### Anti-Patterns (Do Not Do This)

- "It compiled, so it is correct" — Compilation is not semantic verification
- "The tests pass" — Tests only verify what they test
- "I will understand it later" — Understanding erodes; do it now
- "AI knows what I meant" — AI guesses; specify explicitly
- "It is just internal tooling" — Today's script becomes tomorrow's critical infrastructure
- "We are moving fast" — Fast without verification is fast toward failure
- "No one will misuse it" — Assume adversarial use

### Red Flag Phrases (Suggest Higher Risk Score)

- "This is just a quick fix"
- "We can patch it later if there is a problem"
- "No one will probably use this edge case"
- "The AI seems confident"

---

## For AI Assistants

### Implicit Behavior (Apply Without Being Asked)

When generating code, automatically:

**For ALL code (High Trust baseline):**
- Verify: does this match the user's intent?
- Check: could this introduce security vulnerabilities?
- Check: are edge cases handled?
- Flag anything risky: "This touches auth/payments/data deletion — want me to do a deeper review?"

**When risk is elevated (Moderate+):**
- Proactively mention the risk level
- Suggest specific tests for edge cases
- Point out integration concerns
- Review error handling thoroughly

**When risk is high (Guarded/Minimal):**
- Explicitly recommend user review before merging
- Suggest peer review
- Provide a verification checklist specific to the change
- Consider: what is the rollback plan if this breaks?

### VID Assessment Format

```
## VID Assessment

**Change**: <what is being changed>
**Risk Score**: <score>/47 — <Trust Level>

| Dimension     | Score | Reasoning |
|---------------|-------|-----------|
| Impact        | X/5   | ...       |
| Reversibility | X/5   | ...       |
| Exposure      | X/5   | ...       |
| Compliance    | X/10  | ...       |

**Escalation**: <None / Yes — reason>

### Verification Checklist
- [ ] <items appropriate for the trust level>

### Findings
- <any issues found>

### Recommendation
- <what to do next>
```

---

## Quick Reference Card

```
IMPACT (I):        1=trivial  2=minor  3=moderate  4=serious  5=critical
REVERSIBILITY (R): 1=instant  2=easy   3=moderate  4=hard     5=impossible
EXPOSURE (E):      1=dev only 2=team   3=company   4=external 5=public
COMPLIANCE (C):    0=none     2=basic  4=moderate   6=significant  8=critical  10=existential

SCORE = (I x 3) + (R x 2) + (E x 2) + C

 0-10  High Trust      5-10 min    Read, verify intent, automated checks
11-20  Moderate Trust   15-30 min   + edge cases, basic security, integration
21-30  Guarded Trust    30-60 min   + adversarial testing, STRIDE, peer review
31-47  Minimal Trust    1-3 hr      + formal verification, 2+ reviewers, sign-offs

ESCALATE if I/R/E >= 4 OR C >= 6 OR I = 5 (-> Minimal Trust)
WHEN UNSURE: Round up, verify more
```

---

*Attribution: Verified Intent Development (VID) Methodology. Created by Oscar Valenzuela (SEMCL.ONE Community). Licensed under CC BY-SA 4.0.*
