#!/usr/bin/env bash
#
# End-to-end install check for ctrlrelay.
#
# Mirrors the manual workflow described in issue #87:
#   1. Build the Python sdist + wheel from the current source tree.
#   2. Install the freshly built wheel into a clean throw-away venv.
#   3. Invoke the installed `ctrlrelay` binary and verify that:
#      - `ctrlrelay version` prints the version declared in pyproject.toml,
#      - `ctrlrelay --help` exits successfully,
#      - `ctrlrelay config validate -c config/orchestrator.yaml.example`
#        accepts the shipped example config.
#
# The script is hermetic: it builds into a temporary directory and tears
# everything down on exit, so it does not pollute the current checkout
# or the user's site-packages.
#
# Usage:
#   scripts/e2e_install_check.sh                       # build + install + check
#   PYTHON=python3.12 scripts/e2e_install_check.sh
#   WHEEL=path/to/ctrlrelay-*.whl scripts/e2e_install_check.sh   # skip build
#
set -euo pipefail

PYTHON="${PYTHON:-python3}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d -t ctrlrelay-e2e-XXXXXX)"
trap 'rm -rf "${WORK_DIR}"' EXIT

log() { printf '\n=== %s ===\n' "$*"; }

cd "${REPO_ROOT}"

log "Using Python: $("${PYTHON}" --version) (from $(command -v "${PYTHON}"))"
log "Repo root:   ${REPO_ROOT}"
log "Work dir:    ${WORK_DIR}"

EXPECTED_VERSION="$(
  "${PYTHON}" - <<'PY'
import re, pathlib, sys
text = pathlib.Path("pyproject.toml").read_text()
m = re.search(r'(?m)^version\s*=\s*"([^"]+)"', text)
if not m:
    sys.exit("could not find version in pyproject.toml")
print(m.group(1))
PY
)"
log "Expected version (pyproject.toml): ${EXPECTED_VERSION}"

if [ -n "${WHEEL:-}" ]; then
  if [ ! -f "${WHEEL}" ]; then
    echo "ERROR: WHEEL=${WHEEL} does not exist" >&2
    exit 1
  fi
  log "Skipping build, using pre-built wheel: ${WHEEL}"
else
  log "Building sdist + wheel into ${WORK_DIR}/dist"
  "${PYTHON}" -m build --outdir "${WORK_DIR}/dist" "${REPO_ROOT}"

  WHEEL="$(ls "${WORK_DIR}/dist"/ctrlrelay-*.whl | head -n1)"
  SDIST="$(ls "${WORK_DIR}/dist"/ctrlrelay-*.tar.gz | head -n1)"
  if [ -z "${WHEEL}" ] || [ -z "${SDIST}" ]; then
    echo "ERROR: build did not produce both a wheel and an sdist" >&2
    ls -la "${WORK_DIR}/dist" >&2 || true
    exit 1
  fi
  log "Built wheel: ${WHEEL}"
  log "Built sdist: ${SDIST}"
fi

log "Creating fresh venv at ${WORK_DIR}/venv"
"${PYTHON}" -m venv "${WORK_DIR}/venv"
VENV_PY="${WORK_DIR}/venv/bin/python"
VENV_BIN="${WORK_DIR}/venv/bin/ctrlrelay"

log "Installing built wheel into venv"
"${VENV_PY}" -m pip install --quiet --upgrade pip
"${VENV_PY}" -m pip install --quiet "${WHEEL}"

if [ ! -x "${VENV_BIN}" ]; then
  echo "ERROR: ctrlrelay binary was not installed at ${VENV_BIN}" >&2
  ls -la "${WORK_DIR}/venv/bin" >&2 || true
  exit 1
fi

log "Smoke check: ctrlrelay version"
ACTUAL_VERSION="$("${VENV_BIN}" version | tr -d '[:space:]')"
if [ "${ACTUAL_VERSION}" != "${EXPECTED_VERSION}" ]; then
  echo "ERROR: version mismatch — installed binary reports '${ACTUAL_VERSION}', pyproject.toml says '${EXPECTED_VERSION}'" >&2
  exit 1
fi
log "OK: installed binary reports version ${ACTUAL_VERSION}"

log "Smoke check: ctrlrelay --help"
"${VENV_BIN}" --help >/dev/null

log "Smoke check: ctrlrelay --version"
"${VENV_BIN}" --version >/dev/null

log "Smoke check: ctrlrelay config validate (example config)"
"${VENV_BIN}" config validate -c "${REPO_ROOT}/config/orchestrator.yaml.example" >/dev/null

log "All e2e checks passed for ctrlrelay ${ACTUAL_VERSION}"
