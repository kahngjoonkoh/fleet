"""~/.config/fleet/config.toml: the other machines this hub's Devices tab shows.

    [[devices]]
    name = "gpu-box"
    ssh = "gpu-box" # any ssh destination: a ~/.ssh/config alias, user@host, ...
"""

import json
import os
import tomllib
from pathlib import Path

PATH = Path(os.environ.get("FLEET_CONFIG", Path.home() / ".config/fleet/config.toml"))


def load():
    try:
        return tomllib.loads(PATH.read_text())
    except FileNotFoundError:
        return {}


def devices():
    return [d for d in load().get("devices", []) if d.get("name") and d.get("ssh")]


def save_devices(items):
    data = load()
    if set(data) - {"devices"}:
        raise SystemExit(f"fleet: {PATH} has settings besides [[devices]]; edit it by hand")
    PATH.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f'\n[[devices]]\nname = {json.dumps(d["name"])}\nssh = {json.dumps(d["ssh"])}\n' for d in items)
    PATH.write_text("# Machines shown on Fleet's Devices tab, besides this one. Edit with `fleet devices`.\n" + body)
