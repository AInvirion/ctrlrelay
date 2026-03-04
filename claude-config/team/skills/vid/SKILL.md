---
name: vid
description: >
  Verified Intent Development (VID) methodology. This skill MUST be applied
  implicitly whenever writing, reviewing, or modifying code. It governs how
  Claude approaches code generation, verification, and risk assessment.
  Also triggered explicitly when the user says "VID check", "risk score",
  "verify this", "what's the risk", "trust level", "sniff test this change",
  or "provenance check".
tools: Bash, Read, Grep, Glob
---

# Verified Intent Development (VID)

VID is the team's development methodology. It addresses the core challenge: AI makes
code generation cheap, but verification remains the bottleneck. Claude must shift from
"generate and hope" to "intend, generate, verify."

**Reference**: Full methodology at `~/Projects/RESEARCH/Verified-Intent-Development/`

## Core Principles (Always Active)

These five principles govern ALL code generation — apply them implicitly, not just when asked.

### 1. Intent Before Generation
Never write code without first understanding what should be built and how correctness
will be verified. Before generating:
- What is the functional requirement? (inputs, outputs, transformation)
- What are the boundaries? (valid inputs, edge cases, error conditions)
- How will we verify it works? (tests, manual checks, integration)

### 2. Graduated Trust
Not all code deserves equal scrutiny. Match verification depth to risk level using
the risk scoring system below.

### 3. Understanding Over Acceptance
Never produce code that the user won't be able to understand. Prefer clear, readable
solutions over clever ones. If a complex approach is needed, explain *why*.

### 4. Provenance Awareness
Track where code comes from. When generating significant code:
- Note that it's AI-generated in commit context
- Flag any patterns borrowed from external sources
- Be explicit about confidence level

### 5. Continuous Calibration
Learn from outcomes. If a verification approach missed a bug, or was excessive for
the risk level, adjust.

---

## Risk Scoring (When to Apply What)

Score every non-trivial code change across four dimensions:

```
Risk Score = (Impact × 3) + (Reversibility × 2) + (Exposure × 2) + Compliance
```

### Dimensions

**Impact (1-5)**: What happens if this code is wrong?
- 1: Cosmetic issue
- 2: Minor annoyance, easy workaround
- 3: User-facing bug, no data loss
- 4: Data corruption, security hole, financial impact
- 5: System failure, safety, legal liability

**Reversibility (1-5)**: How hard is it to undo?
- 1: Instant (stateless, redeploy)
- 2: Easy (cache clear, restart)
- 3: Moderate (DB migration rollback)
- 4: Hard (manual multi-system correction)
- 5: Impossible (data loss, sent emails, external side effects)

**Exposure (1-5)**: Who sees this code's effects?
- 1: Developer only
- 2: Team internal
- 3: Company internal
- 4: External limited (beta users)
- 5: Public / all users

**Compliance (0-10)**: Regulatory requirements?
- 0: None
- 2-4: Basic standards, GDPR, accessibility
- 6-8: HIPAA, PCI-DSS, financial
- 10: Life-safety, critical infrastructure

### Trust Levels

| Score | Trust Level | Verification Time | What to Do |
|-------|-------------|-------------------|------------|
| 0-10  | High        | 5-10 min          | Read code, verify intent, run automated checks |
| 11-20 | Moderate    | 15-30 min         | + edge case testing, basic security review |
| 21-30 | Guarded     | 30-60 min         | + adversarial testing, security audit, peer review |
| 31-47 | Minimal     | 1-3+ hours        | + formal verification, multiple reviewers, sign-offs |

**Escalation rule**: If ANY dimension scores >= 4, OR Compliance >= 6, move to the next trust level regardless of total score.

---

## Implicit Behavior (Apply Without Being Asked)

When generating code, Claude should automatically:

### For ALL Code (High Trust baseline)
- Mentally verify: does this match the user's intent?
- Check: could this introduce security vulnerabilities? (injection, auth bypass, data exposure)
- Check: are edge cases handled? (empty inputs, nulls, boundary values)
- Flag anything that feels risky: "This touches auth/payments/data deletion — want me to do a deeper review?"

### When Risk is Elevated (Moderate+)
- Proactively mention the risk level: "This is a Moderate trust change because..."
- Suggest specific tests for edge cases
- Point out integration concerns with existing code
- Review error handling thoroughly

### When Risk is High (Guarded/Minimal)
- Explicitly recommend the user review before merging
- Suggest peer review
- Provide a verification checklist specific to the change
- Consider: what's the rollback plan if this breaks?

---

## Explicit Verification (When User Asks)

### "VID check" / "risk score this"

Score the current change or file:

1. **Identify the change scope** — what files/functions are affected
2. **Score all 4 dimensions** — with brief justification for each
3. **Calculate risk score** and map to trust level
4. **Apply the appropriate verification checklist** (see below)
5. **Report findings**

Output format:
```
## VID Assessment

**Change**: <what's being changed>
**Risk Score**: <score>/47 — <Trust Level>

| Dimension    | Score | Reasoning |
|-------------|-------|-----------|
| Impact      | X/5   | ... |
| Reversibility | X/5 | ... |
| Exposure    | X/5   | ... |
| Compliance  | X/10  | ... |

**Escalation**: <None / Yes — reason>

### Verification Checklist
- [ ] <items appropriate for the trust level>

### Findings
- <any issues found during review>

### Recommendation
- <what to do next>
```

### "verify this" / "review this code"

Perform verification appropriate to the risk level:

**Functional Verification:**
- Input space partitioning — are all input categories covered?
- Boundary value analysis — tested at boundaries and boundary ±1?
- State transitions — are all valid/invalid state changes handled?

**Security Verification:**
- Input vectors — every path where external data enters
- Injection points — SQL, command, HTML, path traversal
- Auth/authz — identity proof and permission checks
- Data exposure — where does sensitive data flow?

**Maintainability Verification:**
- Stranger test — would someone understand this in 6 months?
- Naming audit — do names tell the story?
- Complexity — excessive nesting, long functions, unclear flow?

### "provenance check"

Check code origin and AI involvement:
```bash
# Check git history for provenance markers
git log --all --format="%H %s" -- <file> | head -20

# Look for provenance comments in code
grep -n "@provenance\|@tool\|@verification\|AI-generated\|co-authored" <file>

# Check for external origins
grep -n "copied from\|ported from\|based on\|adapted from\|originally from" <file>
```

---

## Verification Checklists by Trust Level

### High Trust (0-10 points)
- [ ] Read code completely
- [ ] Verify it matches stated intent
- [ ] Run existing automated checks (lint, types, tests)
- [ ] 30-second "what could go wrong?" reflection

### Moderate Trust (11-20 points)
- [ ] All High Trust items
- [ ] Trace logic mentally through main paths
- [ ] Test edge cases systematically (nulls, empty, boundaries)
- [ ] Check integration with existing code
- [ ] Basic security review (inputs validated? auth checked?)
- [ ] Document understanding of non-obvious logic

### Guarded Trust (21-30 points)
- [ ] All Moderate Trust items
- [ ] Adversarial thinking — actively try to break it
- [ ] Security audit (STRIDE: Spoofing, Tampering, Repudiation, Info Disclosure, DoS, Elevation)
- [ ] Performance review (O(n) complexity, resource usage, connection handling)
- [ ] Maintainability review (stranger test, change impact)
- [ ] Recommend peer review

### Minimal Trust (31-47 points)
- [ ] All Guarded Trust items
- [ ] Require 2+ independent reviewers
- [ ] Formal security review with tools
- [ ] Mutation testing (target: 90%+ kill rate)
- [ ] Comprehensive documentation
- [ ] Tech lead sign-off
- [ ] Rollback plan documented

---

## Test Verification

Tests need verification too — don't trust AI-generated tests blindly:

1. **Does each test actually test what it claims?** Read the assertion, not just the name
2. **Are assertions correct?** Wrong expected values = false confidence
3. **What's NOT tested?** Edge cases, error paths, security scenarios
4. **Behavior vs implementation?** Tests should verify outcomes, not internal details
5. **Mutation check**: Would the test catch a bug if one line of the implementation changed?

---

## Commit Conventions

When committing AI-assisted code, include provenance context:

```
feat(auth): add JWT token refresh logic

AI-assisted: token rotation logic generated with Claude, verified
against OWASP session management guidelines.

Risk: Moderate (Impact:3, Reversibility:2, Exposure:4, Compliance:2 = 19)
Verification: edge case testing, security review of token handling
```

---

## Decision Quick Reference

**Should I flag the risk level?**
- Trivial change (config, typo, formatting) → No, just do it
- Standard feature work → Mentally assess, flag if Moderate+
- Anything touching auth, payments, data deletion, external APIs → Always flag

**Should I suggest tests?**
- High Trust → Only if no tests exist for the area
- Moderate+ → Yes, suggest specific test cases
- Guarded+ → Suggest tests AND verification approach

**Should I recommend peer review?**
- High/Moderate Trust → No (unless user asks)
- Guarded Trust → Yes, recommend it
- Minimal Trust → Yes, require it
