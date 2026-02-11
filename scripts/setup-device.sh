#!/usr/bin/env bash
set -euo pipefail

# setup-device.sh — Bootstrap a new device for development
# Detects platform (macOS / ChromeOS / Termux) and sets up everything

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${BLUE}[setup]${NC} $*"; }
ok()   { echo -e "${GREEN}  OK${NC} $*"; }
warn() { echo -e "${YELLOW}  WARN${NC} $*"; }
err()  { echo -e "${RED}  ERR${NC} $*"; }
section() { echo ""; echo -e "${CYAN}=== $* ===${NC}"; echo ""; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SYNC_ROOT="$(dirname "$SCRIPT_DIR")"

# --- Platform Detection ---
detect_platform() {
    if [[ -d "/data/data/com.termux" ]]; then
        echo "termux"
    elif [[ "$(uname)" == "Darwin" ]]; then
        echo "macos"
    elif [[ -f "/etc/lsb-release" ]] && grep -q "Chrome" /etc/lsb-release 2>/dev/null; then
        echo "chromeos"
    else
        echo "linux"
    fi
}

PLATFORM="$(detect_platform)"
log "Detected platform: ${GREEN}${PLATFORM}${NC}"

# --- Step 1: Platform-specific prerequisites ---
section "Prerequisites"

case "$PLATFORM" in
    termux)
        log "Installing Termux packages..."
        pkg update -y 2>/dev/null || true
        pkg install -y git openssh nodejs-lts python 2>/dev/null || {
            warn "Some packages may need manual install: pkg install git openssh nodejs-lts python"
        }
        # Termux storage permission
        if [[ ! -d "$HOME/storage" ]]; then
            warn "Run 'termux-setup-storage' first for shared storage access"
        fi
        ;;
    chromeos)
        log "ChromeOS Linux container detected"
        if command -v apt-get &>/dev/null; then
            sudo apt-get update -qq
            sudo apt-get install -y -qq git openssh-client curl 2>/dev/null || true
        fi
        # Install Node.js if missing
        if ! command -v node &>/dev/null; then
            log "Installing Node.js via nvm..."
            curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.1/install.sh | bash
            export NVM_DIR="$HOME/.nvm"
            [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
            nvm install --lts
        fi
        ;;
    macos)
        ok "macOS — assuming dev tools are installed"
        ;;
    linux)
        ok "Linux — assuming git and node are available"
        ;;
esac

# Verify git
if ! command -v git &>/dev/null; then
    err "git is required but not installed"
    exit 1
fi
ok "git: $(git --version)"

# Verify SSH
if [[ ! -f "$HOME/.ssh/id_ed25519" ]] && [[ ! -f "$HOME/.ssh/id_rsa" ]]; then
    section "SSH Key Setup"
    warn "No SSH key found. Generating one..."
    read -rp "Email for SSH key (GitHub email): " ssh_email
    ssh-keygen -t ed25519 -C "$ssh_email" -f "$HOME/.ssh/id_ed25519" -N ""
    echo ""
    log "Add this key to GitHub (https://github.com/settings/keys):"
    echo ""
    cat "$HOME/.ssh/id_ed25519.pub"
    echo ""
    read -rp "Press Enter after adding the key to GitHub..."
else
    ok "SSH key found"
fi

# Test GitHub SSH
log "Testing GitHub SSH access..."
if ssh -T git@github.com 2>&1 | grep -q "successfully authenticated"; then
    ok "GitHub SSH auth works"
else
    warn "GitHub SSH test returned unexpected output (may still work)"
fi

# --- Step 2: Create Projects directory ---
section "Project Directory"

PROJECTS_DIR="${PROJECTS_DIR:-$HOME/Projects}"
mkdir -p "$PROJECTS_DIR"
ok "Projects dir: $PROJECTS_DIR"

# --- Step 3: Sync all repos ---
section "Repository Sync"

export PROJECTS_DIR
bash "$SCRIPT_DIR/sync-repos.sh"

# --- Step 4: Import Claude config ---
section "Claude Code Configuration"

if command -v claude &>/dev/null; then
    ok "Claude Code is installed: $(claude --version 2>/dev/null || echo 'unknown version')"
    bash "$SCRIPT_DIR/sync-claude.sh" import
else
    warn "Claude Code not installed."
    case "$PLATFORM" in
        termux)
            log "Install with: npm install -g @anthropic-ai/claude-code"
            ;;
        *)
            log "Install with: npm install -g @anthropic-ai/claude-code"
            ;;
    esac
    log "After installing, run: $SCRIPT_DIR/sync-claude.sh import"
fi

# --- Step 5: Platform-specific notes ---
section "Platform Notes"

case "$PLATFORM" in
    termux)
        cat <<'NOTES'
Termux-specific tips:
  - Use 'termux-setup-storage' for access to shared storage
  - If MCP tools need native deps, use: pkg install build-essential
  - For background work: install termux-services
  - Keyboard tip: install Hacker's Keyboard from F-Droid
  - Volume-Up + Q = extra keys toolbar
NOTES
        ;;
    chromeos)
        cat <<'NOTES'
ChromeOS-specific tips:
  - Files in ~/Projects are inside the Linux container
  - To access from Files app: /mnt/chromeos/MyFiles/
  - Use Ctrl+Alt+T for crosh, then 'vmc start termina' if container issues
NOTES
        ;;
    macos)
        ok "All set on macOS"
        ;;
esac

echo ""
log "${GREEN}Setup complete!${NC}"
log "Quick commands:"
echo "  Sync repos:   $SCRIPT_DIR/sync-repos.sh"
echo "  Repo status:  $SCRIPT_DIR/sync-repos.sh --status"
echo "  Export config: $SCRIPT_DIR/sync-claude.sh export"
echo "  Import config: $SCRIPT_DIR/sync-claude.sh import"
