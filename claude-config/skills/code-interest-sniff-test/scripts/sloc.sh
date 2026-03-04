#!/usr/bin/env bash
# sloc.sh — Count source lines of code (excluding blanks and comments).
#
# Usage:
#   sloc.sh <file>           Count SLOC for a single file
#   sloc.sh --scan <dir>     Find and count SLOC for all source files in a directory
#
# Output (single file):   <sloc_count>  <file>
# Output (scan mode):     <sloc_count>  <file>   (one per line, sorted by SLOC desc)

set -euo pipefail

# Source file extensions to include
SRC_EXTS="py,js,ts,tsx,jsx,go,rs,c,cpp,h,hpp,java,rb,php,swift,kt,scala,cs,sh,bash,zsh,pl,pm,r,R,lua,ex,exs,erl,hrl,hs,ml,mli,clj,cljs,dart,v,sv,vhd,zig,nim,cr,jl"

# Directories to always exclude
EXCLUDE_DIRS="node_modules|vendor|third_party|external|__pycache__|.git|.venv|venv|env|dist|build|target|.next|.nuxt|coverage|.tox|.mypy_cache|.ruff_cache"

# File patterns to always exclude (generated/config)
EXCLUDE_PATTERNS="\.(min\.|bundle\.|generated\.|pb\.)|(package-lock|yarn\.lock|Pipfile\.lock|poetry\.lock|Cargo\.lock|go\.sum|pnpm-lock)"

count_sloc() {
  local file="$1"
  local ext="${file##*.}"

  # Pick comment pattern based on extension
  local line_comment="#"
  local block_start=""
  local block_end=""

  case "$ext" in
    py|rb|pl|pm|sh|bash|zsh|r|R|jl|cr|nim)
      line_comment="#"
      ;;
    js|ts|tsx|jsx|go|rs|c|cpp|h|hpp|java|swift|kt|scala|cs|dart|v|sv|zig)
      line_comment="//"
      block_start="/\*"
      block_end="\*/"
      ;;
    lua)
      line_comment="--"
      block_start="--\[\["
      block_end="\]\]"
      ;;
    hs|ml|mli)
      line_comment="--"
      block_start="{-"
      block_end="-}"
      ;;
    ex|exs|erl|hrl)
      line_comment="%"
      ;;
    clj|cljs)
      line_comment=";"
      ;;
    php)
      line_comment="//"
      block_start="/\*"
      block_end="\*/"
      ;;
    vhd)
      line_comment="--"
      ;;
    *)
      line_comment="#"
      ;;
  esac

  # Count non-blank, non-comment lines
  # Simple approach: strip blank lines and single-line comments
  # Block comments are harder — use a simple state machine via awk
  if [[ -n "$block_start" ]]; then
    awk -v ls="$line_comment" -v bs="$block_start" -v be="$block_end" '
    BEGIN { in_block = 0; count = 0 }
    {
      # Handle block comments
      if (in_block) {
        if ($0 ~ be) { in_block = 0 }
        next
      }
      if ($0 ~ bs) {
        if ($0 ~ be) { next }  # Single-line block comment
        in_block = 1
        next
      }
      # Skip blank lines
      if ($0 ~ /^[[:space:]]*$/) next
      # Skip line comments (trim leading whitespace first)
      line = $0
      gsub(/^[[:space:]]+/, "", line)
      if (ls == "//" && substr(line, 1, 2) == "//") next
      if (ls == "#" && substr(line, 1, 1) == "#") next
      if (ls == "--" && substr(line, 1, 2) == "--") next
      if (ls == "%" && substr(line, 1, 1) == "%") next
      if (ls == ";" && substr(line, 1, 1) == ";") next
      count++
    }
    END { print count }
    ' "$file"
  else
    awk -v ls="$line_comment" '
    BEGIN { count = 0 }
    {
      if ($0 ~ /^[[:space:]]*$/) next
      line = $0
      gsub(/^[[:space:]]+/, "", line)
      if (ls == "#" && substr(line, 1, 1) == "#") next
      if (ls == "--" && substr(line, 1, 2) == "--") next
      if (ls == "%" && substr(line, 1, 1) == "%") next
      if (ls == ";" && substr(line, 1, 1) == ";") next
      count++
    }
    END { print count }
    ' "$file"
  fi
}

# --- Main ---

if [[ "${1:-}" == "--scan" ]]; then
  DIR="${2:-.}"

  # Build find extension pattern
  EXT_PATTERN=$(echo "$SRC_EXTS" | tr ',' '\n' | sed 's/^/-name "*./' | sed 's/$/"/' | paste -sd ' -o ' -)

  # Find source files, exclude vendored/generated dirs and patterns
  eval "find '$DIR' -type f \( $EXT_PATTERN \)" 2>/dev/null | \
    grep -Ev "($EXCLUDE_DIRS)" | \
    grep -Ev "($EXCLUDE_PATTERNS)" | \
    while IFS= read -r file; do
      sloc=$(count_sloc "$file")
      printf "%6d  %s\n" "$sloc" "$file"
    done | sort -rn

elif [[ -n "${1:-}" ]]; then
  FILE="$1"
  if [[ ! -f "$FILE" ]]; then
    echo "Error: file not found: $FILE" >&2
    exit 1
  fi
  sloc=$(count_sloc "$FILE")
  printf "%6d  %s\n" "$sloc" "$FILE"

else
  echo "Usage: sloc.sh <file> | sloc.sh --scan <directory>" >&2
  exit 1
fi
