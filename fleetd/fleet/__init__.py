"""Fleet: fleetd on every host, fleet-web on the hub."""

import os
from pathlib import Path

STATE_DIR = Path(os.environ.get("FLEET_STATE_DIR", Path.home() / ".local/state/fleet"))
SOCKET = STATE_DIR / "fleetd.sock"
