#!/bin/sh
# Installs or updates Fleet. Rerun the same line to update.
#
#   curl -fsSL https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/install.sh | sh
#       a host: fleetd only
#   curl -fsSL https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/install.sh | sh -s -- --hub --origin https://fleet.example.com
#       the hub: fleetd + fleet-web (--origin only the first time)
#
# Piped like that, it keeps a copy of Fleet in ~/.local/share/fleet/src (with git if it's there, otherwise
# from GitHub's tarball). Run as ./install.sh from a
# checkout, it installs that checkout instead. Nothing here needs sudo.
set -eu

REPO=${FLEET_REPO:-https://github.com/kahngjoonkoh/fleet.git}
BRANCH=${FLEET_BRANCH:-main}
SRC=${FLEET_SRC:-$HOME/.local/share/fleet/src}

say() { printf 'fleet: %s\n' "$*"; }
die() { printf 'fleet: %s\n' "$*" >&2; exit 1; }

# Without git: GitHub's tarball, replacing the old copy but keeping the hub's settings. .fleet-commit
# says which commit it is (the tarball's header has it), for the version shown on the Devices tab.
download() {
  for tool in curl tar gzip; do
    command -v "$tool" >/dev/null || die "needs git, or else curl, tar and gzip ($tool is missing)"
  done
  case $REPO in
    https://github.com/*) ;;
    *) die "needs git to download from $REPO" ;;
  esac
  say "downloading Fleet into $SRC"
  mkdir -p "$(dirname "$SRC")"
  tmp=$(mktemp -d "$(dirname "$SRC")/.fleet-download.XXXXXX")
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL "${REPO%.git}/archive/refs/heads/$BRANCH.tar.gz" -o "$tmp/src.tar.gz" || die "couldn't download Fleet"
  mkdir "$tmp/new"
  tar -xzf "$tmp/src.tar.gz" -C "$tmp/new" --strip-components=1
  gzip -dc "$tmp/src.tar.gz" | head -c 1024 | tr -d '\000' |
    sed -n 's/.*comment=\([0-9a-f]\{40\}\).*/\1/p' > "$tmp/new/.fleet-commit"
  if [ -f "$SRC/.env" ]; then cp -p "$SRC/.env" "$tmp/new/.env"; fi
  rm -rf "$SRC"
  mv "$tmp/new" "$SRC"
  rm -rf "$tmp"
  trap - EXIT
}

if [ "$(basename "$0")" = install.sh ] && [ -f "$(dirname "$0")/fleetd/pyproject.toml" ]; then
  SRC=$(cd "$(dirname "$0")" && pwd)
else
  if [ -d "$SRC/.git" ]; then
    command -v git >/dev/null || die "$SRC is a git clone, and git isn't installed"
    say "updating $SRC"
    git -C "$SRC" pull --ff-only --quiet
  elif command -v git >/dev/null && [ ! -f "$SRC/.fleet-commit" ]; then
    say "downloading Fleet into $SRC"
    mkdir -p "$(dirname "$SRC")"
    git clone --quiet --depth 1 --branch "$BRANCH" "$REPO" "$SRC"
  else
    [ -f "$SRC/.fleet-commit" ] || [ ! -e "$SRC" ] || die "$SRC is already there and isn't a copy of Fleet"
    download
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
