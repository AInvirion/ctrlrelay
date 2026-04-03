# VID Templates Reference

> Ready-to-use templates for project specifications, verification rituals, and risk assessment.
> Source: Verified Intent Development (VID) Methodology — CC BY-SA 4.0

## Table of Contents

1. [Quick Intent Template](#quick-intent-template)
2. [Extended Intent Template](#extended-intent-template)
3. [Formal Specification Template](#formal-specification-template)
4. [Risk Classification Worksheet](#risk-classification-worksheet)
5. [Verification Ritual Checklists](#verification-ritual-checklists)
6. [Provenance Annotation Guide](#provenance-annotation-guide)
7. [Commit Message Format](#commit-message-format)

---

## Quick Intent Template (2-3 minutes)

Use before generating any code in a High Trust context:

```markdown
## Intent Specification

**Goal:** [One sentence: what you need to build]

**Requirements:**
- [ ] [Functional requirement 1]
- [ ] [Functional requirement 2]
- [ ] [Functional requirement 3]

**Edge Cases to Handle:**
- [Edge case 1]
- [Edge case 2]

**Success Criteria:**
- [How I will know it works]

**Risk Level:** [High Trust / Moderate / Guarded / Minimal] (Score: ___)

**Verification Plan:**
- [How I will verify this code]
```

**Example:**

```markdown
## Intent Specification

**Goal:** Create a function to validate email addresses for user registration

**Requirements:**
- [ ] Accept string input
- [ ] Return boolean (valid/invalid)
- [ ] Follow RFC 5322 standard
- [ ] Block disposable email domains

**Edge Cases to Handle:**
- Empty string
- Very long emails (>254 chars)
- International characters
- Multiple @ symbols

**Success Criteria:**
- Rejects invalid@
- Rejects @invalid.com
- Accepts valid@example.com
- Handles unicode correctly

**Risk Level:** Moderate Trust (Score: 15)
- Impact: 3 (affects registration)
- Reversibility: 2 (easy to fix)
- Exposure: 2 (all users)
- Compliance: 0

**Verification Plan:**
- Test with 20+ edge cases
- Verify against RFC 5322
- Check disposable domain list coverage
```

---

## Extended Intent Template (5-10 minutes)

Use for Moderate Trust and above:

```markdown
## Intent Specification (Extended)

**Task ID:** [Reference number if applicable]

**Goal:** [Detailed description of what needs to be built]

**Context:**
- **Why this is needed:** [Business/technical justification]
- **Current state:** [What exists now]
- **Desired state:** [What should exist after]
- **Related systems:** [What this integrates with]

**Functional Requirements:**
- [ ] [Requirement 1]
- [ ] [Requirement 2]
- [ ] [Requirement 3]

**Non-Functional Requirements:**
- **Performance:** [Response time, throughput, etc.]
- **Security:** [Authentication, authorization, data protection]
- **Compliance:** [Regulatory requirements]
- **Scalability:** [Expected load, growth]
- **Reliability:** [Uptime requirements, error handling]

**Edge Cases & Error Conditions:**
- [Edge case 1]: [Expected behavior]
- [Edge case 2]: [Expected behavior]
- [Error condition 1]: [How to handle]

**Success Criteria:**
- **Functional:** [Observable behaviors that must work]
- **Performance:** [Measurable targets]
- **Quality:** [Code quality expectations]

**Risk Assessment:**
| Factor | Score | Rationale |
|--------|-------|-----------|
| Impact | _ / 5 | [What happens if this is wrong?] |
| Reversibility | _ / 5 | [How easy to fix?] |
| Exposure | _ / 5 | [Who is affected?] |
| Compliance | _ / 10 | [Regulatory implications?] |
| **Total** | **__** | **Trust Level: _______** |

**Verification Plan:**
- [ ] [Verification step 1]
- [ ] [Verification step 2]
- [ ] [Verification step 3]

**Time Estimates:**
- Generation: [Expected AI generation time]
- Verification: [Allocated verification time based on risk]
- Total: [Combined estimate]

**Notes:**
[Any additional context, concerns, or considerations]
```

---

## Formal Specification Template

Use for Guarded Trust (21+) and all critical components:

```markdown
## Intent Specification: [Feature/Function Name]

**Author:** [Your name]
**Date:** [Date]
**Risk Level:** [Trust Level]
**Risk Score:** [0-47] (Impact: X, Reversibility: X, Exposure: X, Compliance: X)

### Purpose
[1-2 sentences: What problem does this code solve?]

### Functional Requirements
1. [Specific requirement with measurable outcome]
2. [Specific requirement with measurable outcome]
3. [...]

### Input Specification
- **Valid inputs:** [Types, ranges, formats]
- **Invalid inputs:** [What should be rejected and how]
- **Edge cases:** [Boundary values, empty inputs, maximum sizes]

### Output Specification
- **Success case:** [What the code returns/produces on success]
- **Error cases:** [What happens for each error condition]
- **Side effects:** [Database changes, API calls, file writes, etc.]

### Non-Functional Requirements
- **Performance:** [Response time, throughput requirements]
- **Security:** [Authentication, authorization, data protection]
- **Compliance:** [GDPR, HIPAA, SOX, etc.]
- **Scalability:** [Expected load, growth projections]

### Success Criteria
**The code is correct if:**
1. [Testable criterion]
2. [Testable criterion]

### Verification Plan
**Based on risk level [X], I will:**
- [ ] [Verification step matching trust level]
- [ ] [Peer review required? Yes/No]
- [ ] [Security review required? Yes/No]

### Dependencies & Integration
- **Depends on:** [Other systems, services, libraries]
- **Used by:** [Callers, consumers]
- **Breaking changes:** [Any backwards compatibility concerns]

### Assumptions & Constraints
- [Assumption 1]
- [Constraint 1]
```

---

## Risk Classification Worksheet

```markdown
## Risk Classification Worksheet

**Task:** [Brief description]
**Date:** [Date of assessment]
**Assessor:** [Your name]

### 1. Impact (Score 1-5)
**Question:** What happens if this code is wrong?
1 = Minor inconvenience (log message incorrect)
2 = User-visible bug (UI glitch)
3 = Significant issue (feature unavailable)
4 = Major problem (data loss, security vuln, revenue impact)
5 = Critical failure (system down, data breach, legal liability)

**Score:** ___ / 5
**Rationale:** [Why this score?]

### 2. Reversibility (Score 1-5)
**Question:** How easily can we fix or undo this?
1 = Instant rollback (feature flag, config change)
2 = Quick fix (code change, redeploy in minutes)
3 = Standard fix (normal deployment process, <1 day)
4 = Difficult fix (migration, data cleanup, coordination)
5 = Nearly irreversible (permanent data loss, legal breach)

**Score:** ___ / 5
**Rationale:** [Why this score?]

### 3. Exposure (Score 1-5)
**Question:** Who is affected?
1 = Only me (dev environment only)
2 = Dev team (internal tools)
3 = Limited users (beta, specific segment)
4 = All users (production, all customers)
5 = Public exposure (external API, security boundary)

**Score:** ___ / 5
**Rationale:** [Why this score?]

### 4. Compliance (Score 0-10)
**Question:** Are there regulatory requirements?
0 = No compliance requirements
2 = Internal policies only
4 = Industry best practices (PCI DSS non-payment)
6 = Regulatory (HIPAA, GDPR, SOX non-critical)
8 = Direct compliance impact (payment, PHI, financial)
10 = Existential compliance risk (FDA, financial transactions)

**Score:** ___ / 10
**Rationale:** [Which regulations apply?]

### Calculation
Impact:        ___ x 3 = ___
Reversibility: ___ x 2 = ___
Exposure:      ___ x 2 = ___
Compliance:    ___     = ___
                 Total = ___

### Trust Level: ___________________
```

---

## Verification Ritual Checklists

### High Trust (5-10 minutes)

- [ ] Code reads clearly
- [ ] Matches intent specification
- [ ] No obvious syntax errors
- [ ] No hardcoded secrets/credentials
- [ ] Appropriate error handling present
- [ ] Run code (manually or via test)
- [ ] Test happy path
- [ ] Test one edge case
- [ ] I understand what this code does
- [ ] Mark as AI-generated in commit

### Moderate Trust (15-30 minutes)

All High Trust items, plus:
- [ ] Read all generated code line by line
- [ ] Review control flow (loops, conditionals)
- [ ] Validate error handling is comprehensive
- [ ] Check for potential null/undefined issues
- [ ] Verify resource cleanup (files, connections)
- [ ] Test 3-5 edge cases
- [ ] Test error conditions
- [ ] No SQL injection vulnerabilities
- [ ] No XSS vulnerabilities
- [ ] Input validation present
- [ ] Integrates correctly with existing code
- [ ] API contracts respected
- [ ] I can explain every line
- [ ] Comments added for complex logic

### Guarded Trust (30-60 minutes)

All Moderate Trust items, plus:
- [ ] Verify algorithm correctness
- [ ] Check for subtle logic bugs
- [ ] Review edge case handling
- [ ] Validate error propagation
- [ ] Check concurrency safety (if applicable)
- [ ] Review performance characteristics
- [ ] Test 10+ edge cases
- [ ] Test all error paths
- [ ] Boundary condition testing
- [ ] Full security review completed
- [ ] Authentication/authorization correct
- [ ] OWASP Top 10 addressed
- [ ] Design pattern appropriate
- [ ] Does not introduce technical debt
- [ ] Scalability considered
- [ ] Peer review by another developer
- [ ] Architecture decision recorded

### Minimal Trust (1-3+ hours)

All Guarded Trust items, plus:
- [ ] Algorithm correctness proven
- [ ] Security reviewed by specialist
- [ ] Performance profiled and verified
- [ ] Concurrency issues ruled out
- [ ] Unit tests for every function/method
- [ ] Integration tests for all integrations
- [ ] End-to-end tests for critical paths
- [ ] Stress testing completed
- [ ] Mutation testing (target >90% kill rate)
- [ ] Regulatory requirements verified
- [ ] 2+ independent reviewers
- [ ] Tech lead sign-off
- [ ] Deployment plan documented
- [ ] Rollback plan documented and tested
- [ ] Monitoring dashboards created
- [ ] Alerting configured
- [ ] On-call team briefed

---

## Provenance Annotation Guide

### Code Comment Annotations

**AI-Generated:**
```python
# @provenance: AI-generated (Claude)
# @verified-by: engineer_name
# @risk-score: 14 (Moderate Trust)
```

**Modified from AI:**
```python
# Originally AI-generated: 2024-06-15 (@sarah, Moderate Trust)
# Modified: 2024-12-07 (@mike, added rate limiting, Guarded Trust)
# Risk score: 25
```

**Human-Written (in AI-augmented codebase):**
```python
# @provenance: Human-written (@alex)
# DO NOT ask AI to refactor without understanding business rules.
```

### Provenance Categories

| Type | When to Use |
|------|-------------|
| AI-generated | Code written entirely by AI |
| Human-written | Code written entirely by human |
| AI-assisted | Human wrote with AI suggestions |
| Migrated | Existing code from pre-AI era |
| Modified from AI | AI-generated, then human-modified |

---

## Commit Message Format

```
[VID] <type>: <description>

<body explaining what and why>

Provenance: <AI-generated | Human-written | AI-assisted | Migrated>
Verification: <High Trust | Moderate | Guarded | Minimal>
Verified-by: <your-username>
Risk-score: <0-47>
Time-spent: <verification time>

[Optional: Issues found and addressed]
[Optional: Review-by: if peer reviewed]
```

**Example:**
```
[VID] feat: Add email validation for user registration

Implements RFC 5322-compliant email validation with disposable
domain blocking for the user registration flow.

Provenance: AI-generated
Verification: Moderate Trust
Verified-by: @sarah
Risk-score: 15
Time-spent: 22 minutes

Issues found and addressed:
- Missing null check (added)
- Disposable domain list was incomplete (updated)
```

---

*Attribution: Verified Intent Development (VID) Methodology. Created by Oscar Valenzuela (SEMCL.ONE Community). Licensed under CC BY-SA 4.0.*
