# GitHub Labels

## Creation Commands

Run before creating issues (`--force` = idempotent):

```bash
gh label create "severity: critical" --color "B60205" --description "Data loss, crash, security breach" --force
gh label create "severity: high" --color "D93F0B" --description "Incorrect behavior, auth issues" --force
gh label create "severity: medium" --color "FBCA04" --description "Edge cases, degraded experience" --force
gh label create "severity: low" --color "0E8A16" --description "Cosmetic, minor inconsistencies" --force
gh label create "type: bug" --color "D73A4A" --description "Logic error, incorrect behavior" --force
gh label create "type: security" --color "EE0701" --description "Vulnerability, auth issue" --force
gh label create "type: ux" --color "1D76DB" --description "User experience issue" --force
gh label create "type: performance" --color "5319E7" --description "Performance bottleneck" --force
```

## Prefix → Label Mapping

| Prefix   | Label              |
|----------|--------------------|
| `BUG-*`  | `type: bug`        |
| `VULN-*` | `type: security`   |
| `UX-*`   | `type: ux`         |
| `PERF-*` | `type: performance`|

Each issue gets exactly **1 severity label** + **1 type label**.
