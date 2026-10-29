#!/bin/sh
# Installs or updates Fleet. Rerun the same line to update.
#
#   curl -fsSL https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/install.sh | sh
#       a host: fleetd only
#   curl -fsSL https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/install.sh | sh -s -- --hub --origin https://fleet.example.com
#       the hub: fleetd + fleet-web (--origin only the first time)
#
# Piped like that, it keeps a copy of Fleet in ~/.local/share/fleet/src. Run as ./install.sh from a
# checkout, it installs that checkout instead. Nothing here needs sudo.
set -eu

REPO=${FLEET_REPO:-https://github.com/kahngjoonkoh/fleet.git}
BRANCH=${FLEET_BRANCH:-main}
SRC=${FLEET_SRC:-$HOME/.local/share/fleet/src}

say() { printf 'fleet: %s\n' "$*"; }
die() { printf 'fleet: %s\n' "$*" >&2; exit 1; }

if [ "$(basename "$0")" = install.sh ] && [ -f "$(dirname "$0")/fleetd/pyproject.toml" ]; then
  SRC=$(cd "$(dirname "$0")" && pwd)
else
  command -v git >/dev/null || die "needs git"
  if [ -d "$SRC/.git" ]; then
    say "updating $SRC"
    git -C "$SRC" pull --ff-only --quiet
  else
    say "downloading Fleet into $SRC"
    mkdir -p "$(dirname "$SRC")"
    git clone --quiet --depth 1 --branch "$BRANCH" "$REPO" "$SRC"
  fi
fi

ORIGINAL_PATH=$PATH
PATH=$HOME/.local/bin:$PATH
if ! command -v uv >/dev/null; then
  command -v curl >/dev/null || die "needs curl to install uv"
  say "installing uv, the Python tool manager, into ~/.local/bin"
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null
fi

uv tool install --force --reinstall --quiet "$SRC/fleetd"
"$(uv tool dir --bin)/fleet" install --repo "$SRC" "$@"

case ":$ORIGINAL_PATH:" in
  *":$HOME/.local/bin:"*) ;;
  *) say "add ~/.local/bin to your PATH to use the fleet command (open a new shell if uv just did)" ;;
esac
