#!/bin/sh
# Installs Fleet's /fleet-push and /fleet-pull skills for Claude Code. Rerun to update; it replaces
# earlier copies of these two skills and nothing else.
#
#   curl -fsSL https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/skills/install-claude.sh | sh
set -eu

BASE=${FLEET_RAW:-https://raw.githubusercontent.com/kahngjoonkoh/fleet/main}/skills/claude
DIR=${CLAUDE_CONFIG_DIR:-$HOME/.claude}/skills

command -v curl >/dev/null || { echo "fleet: needs curl" >&2; exit 1; }
for name in fleet-push fleet-pull; do
  mkdir -p "$DIR/$name"
  tmp=$(mktemp "$DIR/$name/.SKILL.XXXXXX")
  if ! curl -fsSL "$BASE/$name/SKILL.md" -o "$tmp"; then
    rm -f "$tmp"
    echo "fleet: couldn't download $name" >&2
    exit 1
  fi
  chmod 644 "$tmp"
  mv "$tmp" "$DIR/$name/SKILL.md"
done
# Their old names.
for old in fleet-save fleet-resume; do
  rm -f "$DIR/$old/SKILL.md"
  rmdir "$DIR/$old" 2>/dev/null || true
done
echo "fleet: installed /fleet-push and /fleet-pull in $DIR. Start a new Claude Code session to use them."
