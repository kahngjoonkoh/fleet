"""Fleet: fleetd on every host, fleet-web on the hub."""

import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("FLEET_STATE_DIR", Path.home() / ".local/state/fleet"))
SOCKET = STATE_DIR / "fleetd.sock"
# Written by `fleet install`: the package version plus the git commit it was installed from.
VERSION_FILE = STATE_DIR / "version"
# Also written by `fleet install`: the checkout it installed from, so the hub can say how to update.
SOURCE_FILE = STATE_DIR / "source"
# Written by `fleet install --hub`: this machine keeps projects and their brains, and serves brain.sock.
HUB_FILE = STATE_DIR / "hub"
# Where updates come from: the Devices tab compares each machine's commit with this repo's main.
GITHUB_REPO = os.environ.get("FLEET_GITHUB_REPO", "kahngjoonkoh/fleet")
