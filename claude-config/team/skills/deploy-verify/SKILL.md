---
name: deploy-verify
description: >
  This skill should be used when the user asks to "deploy", "push and deploy",
  "verify deployment", "check deployment status", "monitor deploy",
  "ship it", "push to production", "deploy to digitalocean", or wants to
  commit, push, and verify a DigitalOcean App Platform deployment.
tools: Bash, Read, Grep
---

# Deploy & Verify — DigitalOcean App Platform

Automates the full cycle: commit → push → wait for deploy → verify logs → report.

## Prerequisites

- `doctl` CLI authenticated (`doctl auth init`)
- `jq` installed
- Git repo with an `origin` remote pointing to GitHub
- The repo must be linked to a DigitalOcean App Platform app

## Workflow

Execute these phases in order. If any phase fails, stop and report the failure clearly.

### Phase 1 — Commit & Push

1. Run `git status` to check for changes. If working tree is clean and there are no staged changes, skip to Phase 2 (the user may just want to check an existing deployment).
2. If there are changes:
   - Show the user what changed with `git status`
   - Ask the user for a commit message if one wasn't provided
   - Stage relevant files (prefer `git add` with specific files over `git add -A`)
   - Create a commit
   - Push to the current branch: `git push origin HEAD`

### Phase 2 — Identify the DO App

1. Get the GitHub repo from the git remote:
   ```bash
   REMOTE_URL=$(git remote get-url origin)
   # Extract owner/repo — handles both HTTPS and SSH URLs
   GITHUB_REPO=$(echo "$REMOTE_URL" | sed -E 's#(https://github\.com/|git@github\.com:)##' | sed 's/\.git$//')
   ```

2. List all DO apps and find the one linked to this repo:
   ```bash
   doctl apps list --output json | jq -r '.[] | select(.spec.services[]?.github?.repo == "'"$GITHUB_REPO"'" or .spec.static_sites[]?.github?.repo == "'"$GITHUB_REPO"'" or .spec.workers[]?.github?.repo == "'"$GITHUB_REPO"'" or .spec.jobs[]?.github?.repo == "'"$GITHUB_REPO"'") | "\(.id) \(.spec.name)"'
   ```

3. If no match is found, list all available apps and ask the user to pick one.

4. Save the **app ID**, **app name**, and **component name** for subsequent commands. Extract the component name from the matching spec entry.

### Phase 3 — Wait for Deployment

Run the polling script:

```bash
bash ~/.claude/skills/deploy-verify/scripts/poll-deploy.sh <APP_ID>
```

The script:
- Polls every 15 seconds for up to 10 minutes
- Prints a status line each cycle
- Exits with code 0 (success), 1 (failure), or 2 (timeout)
- Outputs the deployment ID on the last line

Capture the deployment ID from the script output for Phase 4.

If the script exits non-zero, report the failure and skip to Phase 5.

### Phase 4 — Verify Logs

Check three log types for the latest deployment. For each, scan for error patterns (`error`, `Error`, `ERROR`, `fatal`, `Fatal`, `FATAL`, `panic`, `exception`, `Exception`, `failed`, `Failed`, `FAILED`, `crash`, `Crash`, `OOMKilled`, `exit code`).

1. **Build logs** — look for compilation errors or warnings:
   ```bash
   doctl apps logs <APP_ID> <COMPONENT> --type build --follow=false 2>&1 | tail -100
   ```

2. **Deploy logs** — look for container/startup failures:
   ```bash
   doctl apps logs <APP_ID> --type deploy --follow=false 2>&1 | tail -100
   ```

3. **Runtime logs** — look for crash loops or runtime errors:
   ```bash
   doctl apps logs <APP_ID> --type run --follow=false 2>&1 | tail -50
   ```

Collect any matched error/warning lines for the report.

### Phase 5 — Report

Present a clear summary:

```
## Deployment Report

- **App**: <app name>
- **App ID**: <app id>
- **Deployment ID**: <deployment id>
- **Status**: <ACTIVE / ERROR / FAILED / TIMEOUT>
- **Duration**: <elapsed time>
- **URL**: <default ingress URL from app spec>

### Errors / Warnings
<any error lines found in logs, or "None found">

### Next Steps
<suggestions if failed, or "Deployment successful — no action needed">
```

To get the app URL:
```bash
doctl apps get <APP_ID> --output json | jq -r '.default_ingress // .live_url // "N/A"'
```

## Notes

- If the user says just "deploy" or "ship it", run all 5 phases.
- If the user says "check deployment" or "verify deployment", skip Phase 1 and start from Phase 2.
- If a deployment is already in progress when we push, the poller will pick it up.
- Always confirm with the user before committing if the commit message wasn't provided.
