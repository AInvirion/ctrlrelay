#!/usr/bin/env bash
# poll-deploy.sh — Poll a DigitalOcean App Platform deployment until completion.
#
# Usage: poll-deploy.sh <app-id> [--timeout <seconds>]
# Exit codes: 0 = ACTIVE (success), 1 = ERROR/FAILED/CANCELED, 2 = timeout

set -euo pipefail

APP_ID="${1:?Usage: poll-deploy.sh <app-id> [--timeout <seconds>]}"
shift

TIMEOUT=600
while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout) TIMEOUT="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

START=$(date +%s)
DEPLOY_ID=""
LAST_PHASE=""

while true; do
  ELAPSED=$(( $(date +%s) - START ))
  if [[ $ELAPSED -ge $TIMEOUT ]]; then
    echo "Timeout after ${TIMEOUT}s waiting for deployment"
    echo "${DEPLOY_ID:-unknown}"
    exit 2
  fi

  # Get latest deployment
  JSON=$(doctl apps list-deployments "$APP_ID" --output json 2>&1) || {
    echo "[$(date +%H:%M:%S)] Error fetching deployments: $JSON" >&2
    sleep 15
    continue
  }

  # Parse latest deployment using python3 (more reliable than jq on macOS)
  DEPLOY_INFO=$(python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
if not data:
    print('NO_DEPLOYMENTS')
    sys.exit(0)
d = data[0]
phase = d.get('phase', 'UNKNOWN')
deploy_id = d.get('id', 'unknown')
progress_steps = d.get('progress', {}).get('steps', [])
done = sum(1 for s in progress_steps if s.get('status') == 'SUCCESS')
total = len(progress_steps)
progress = f'{done}/{total} steps' if total > 0 else 'starting'
print(f'{deploy_id}|{phase}|{progress}')
" <<< "$JSON" 2>/dev/null) || {
    echo "[$(date +%H:%M:%S)] Error parsing deployment data" >&2
    sleep 15
    continue
  }

  if [[ "$DEPLOY_INFO" == "NO_DEPLOYMENTS" ]]; then
    echo "[$(date +%H:%M:%S)] No deployments found yet..."
    sleep 15
    continue
  fi

  IFS='|' read -r DEPLOY_ID PHASE PROGRESS <<< "$DEPLOY_INFO"

  # Print status line
  ELAPSED_FMT=$(printf '%dm%02ds' $((ELAPSED/60)) $((ELAPSED%60)))
  echo "[$(date +%H:%M:%S)] Deployment ${DEPLOY_ID:0:12}...: ${PHASE} (${PROGRESS}) [${ELAPSED_FMT}]"

  case "$PHASE" in
    ACTIVE)
      echo "Deployment successful!"
      echo "$DEPLOY_ID"
      exit 0
      ;;
    ERROR|FAILED|CANCELED)
      echo "Deployment ${PHASE}!"
      echo "$DEPLOY_ID"
      exit 1
      ;;
    SUPERSEDED)
      echo "Deployment was superseded by a newer one"
      echo "$DEPLOY_ID"
      exit 1
      ;;
  esac

  LAST_PHASE="$PHASE"
  sleep 15
done
