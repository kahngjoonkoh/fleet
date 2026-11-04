#!/bin/sh
# Installs Fleet's /fleet-push and /fleet-pull commands for pi. Rerun to update; it replaces earlier
# copies of these two commands and nothing else.
#
#   curl -fsSL https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/skills/install-pi.sh | sh
set -eu

BASE=${FLEET_RAW:-https://raw.githubusercontent.com/kahngjoonkoh/fleet/main}/skills/pi
DIR=${PI_CODING_AGENT_DIR:-$HOME/.pi/agent}/prompts

command -v curl >/dev/null || { echo "fleet: needs curl" >&2; exit 1; }
mkdir -p "$DIR"
for name in fleet-push fleet-pull; do
  tmp=$(mktemp "$DIR/.$name.XXXXXX")
  if ! curl -fsSL "$BASE/$name.md" -o "$tmp"; then
    rm -f "$tmp"
    echo "fleet: couldn't download $name" >&2
    exit 1
  fi
  chmod 644 "$tmp"
  mv "$tmp" "$DIR/$name.md"
done
# Their old names.
rm -f "$DIR/fleet-save.md" "$DIR/fleet-resume.md"
echo "fleet: installed /fleet-push and /fleet-pull in $DIR. Run /reload in a pi session that's already open."
