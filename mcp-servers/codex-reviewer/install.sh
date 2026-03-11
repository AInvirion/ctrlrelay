#!/usr/bin/env bash
set -euo pipefail

# Install codex-reviewer MCP server

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
CLAUDE_JSON="$HOME/.claude.json"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

log()  { echo -e "${BLUE}[codex-reviewer]${NC} $*"; }
ok()   { echo -e "${GREEN}  OK${NC} $*"; }
warn() { echo -e "${YELLOW}  WARN${NC} $*"; }
err()  { echo -e "${RED}  ERROR${NC} $*"; }

# Check prerequisites
log "Checking prerequisites..."

if ! command -v node &> /dev/null; then
    err "Node.js is required but not installed"
    exit 1
fi
ok "Node.js $(node --version)"

if ! command -v codex &> /dev/null; then
    err "Codex CLI is required but not installed"
    echo "Install with: npm install -g @openai/codex"
    exit 1
fi
ok "Codex CLI found"

# Install dependencies
log "Installing dependencies..."
cd "$SCRIPT_DIR"
npm install --silent
ok "Dependencies installed"

# Make executable
chmod +x "$SCRIPT_DIR/index.js"

# Add to Claude's MCP servers
log "Configuring Claude Code..."

if [[ ! -f "$CLAUDE_JSON" ]]; then
    echo '{}' > "$CLAUDE_JSON"
fi

# Use Python to safely update JSON
python3 << PYTHON
import json
import os

claude_json_path = os.path.expanduser("$CLAUDE_JSON")
script_dir = "$SCRIPT_DIR"

# Read existing config
try:
    with open(claude_json_path) as f:
        config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    config = {}

# Ensure mcpServers exists
if "mcpServers" not in config:
    config["mcpServers"] = {}

# Add codex-reviewer server
config["mcpServers"]["codex-reviewer"] = {
    "command": "node",
    "args": [f"{script_dir}/index.js"]
}

# Write back
with open(claude_json_path, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")

print("  OK  Added codex-reviewer to ~/.claude.json")
PYTHON

echo ""
log "Installation complete!"
echo ""
echo "Available tools in Claude Code:"
echo "  - codex_review          General code review"
echo "  - codex_security_review Security-focused review"
echo "  - codex_find_duplicates Find copy-paste code"
echo "  - codex_find_dead_code  Find unused code"
echo "  - codex_verify_fixes    Verify issues are fixed"
echo "  - codex_test_coverage   Analyze test gaps"
echo "  - codex_dependency_audit Audit dependencies"
echo "  - codex_performance_review Performance issues"
echo "  - codex_prompt          Custom Codex prompt"
echo ""
echo "Restart Claude Code to use the new tools."
