"""fleet: installs and manages Fleet on this machine.

  fleet install                 fleetd as a systemd user service (every host)
  fleet install --hub           the above, plus fleet-web in Docker (the hub only)
      [--show-hardware]           also show CPU, memory, disk and GPUs on the Devices tab (off by default)
  fleet status                  is fleetd up, and on the hub, fleet-web
  fleet uninstall               stops and removes fleetd (and fleet-web on the hub; its data volume is kept)
  fleet admin create EMAIL      first or extra admin account on the hub; prints a one-time password
  fleet admin reset-password EMAIL
  fleet devices                 machines on the Devices tab (besides this one)
  fleet devices add NAME [--ssh DEST]   DEST defaults to NAME; must work with plain `ssh DEST`
  fleet devices remove NAME
"""

import argparse
import getpass
import importlib.metadata
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import HUB_FILE, SOCKET, SOURCE_FILE, STATE_DIR, VERSION_FILE, config

UNIT_PATH = Path.home() / ".config/systemd/user/fleetd.service"
UNIT = """\
# Written by `fleet install`; rerun that instead of editing this file.
[Unit]
Description=fleetd, the Fleet host daemon

[Service]
ExecStart={exe}
# The socket is created rw for this user's group, which fleet-web joins.
UMask=0007
Restart=on-failure
RestartSec=2
# Restarting fleetd for an update must not take down a tmux server started from its terminal.
KillMode=process

[Install]
WantedBy=default.target
"""


def say(msg):
    print(f"fleet: {msg}", flush=True)


def fail(msg):
    print(f"fleet: {msg}", file=sys.stderr)
    sys.exit(1)


def run(*cmd, check=True, capture=False, **kw):
    return subprocess.run(cmd, check=check, text=True, capture_output=capture, **kw)


def systemctl(*args, check=True, capture=False):
    # Works from shells that lack a user session's environment, such as tmux started over sudo or cron.
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return run("systemctl", "--user", *args, check=check, capture=capture, env=env)


def fleetd_exe():
    exe = shutil.which("fleetd") or str(Path(sys.argv[0]).resolve().parent / "fleetd")
    if not Path(exe).exists():
        fail("can't find the fleetd executable; install with ./install.sh from the Fleet repo")
    return exe


def fleetd_healthy():
    try:
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(2)
            s.connect(str(SOCKET))
            s.sendall(b"GET /health HTTP/1.0\r\n\r\n")
            return s.recv(64).split(b" ", 2)[1:2] == [b"200"]
    except OSError:
        return False


def linger_on():
    out = run("loginctl", "show-user", getpass.getuser(), "-p", "Linger", "--value", check=False, capture=True)
    return out.stdout.strip() == "yes"


def build_version(repo):
    """e.g. 0.1.0+bcb752b, or 0.1.0+bcb752b.dirty when installed from a checkout with local edits."""
    version = importlib.metadata.version("fleet")
    if repo:
        git = lambda *a: run("git", "-C", str(repo), *a, check=False, capture=True).stdout.strip()
        if commit := git("rev-parse", "--short", "HEAD"):
            version += f"+{commit}" + (".dirty" if git("status", "--porcelain") else "")
    return version


def install_fleetd(version, repo):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o750)
    VERSION_FILE.write_text(version + "\n")
    SOURCE_FILE.write_text(f"{repo.resolve()}\n" if repo else "")
    UNIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    UNIT_PATH.write_text(UNIT.format(exe=fleetd_exe()))
    systemctl("daemon-reload")
    systemctl("enable", "--quiet", "fleetd")
    systemctl("restart", "fleetd")
    for _ in range(25):
        if fleetd_healthy():
            say(f"fleetd {version} is running ({SOCKET})")
            break
        time.sleep(0.2)
    else:
        fail("fleetd didn't start; see: journalctl --user -u fleetd -n 50")
    if not linger_on():
        say("note: fleetd only starts once you log in after a reboot. To start it at boot, run once:")
        print(f"    sudo loginctl enable-linger {getpass.getuser()}")


def find_repo(given):
    for d in ([Path(given)] if given else [Path.cwd(), *Path.cwd().parents]):
        if (d / "compose.yaml").exists() and (d / "web").is_dir():
            return d.resolve()
    fail("run this from the Fleet repo, or pass --repo DIR")


def read_env(path):
    env = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    return env


def install_hub(repo, origin, show_hardware, version):
    if not shutil.which("docker"):
        fail("the hub runs fleet-web in Docker, and docker isn't installed")
    env_path = repo / ".env"
    env = read_env(env_path)
    env["FLEET_PUBLIC_ORIGIN"] = (origin or env.get("FLEET_PUBLIC_ORIGIN", "")).rstrip("/")
    if not env["FLEET_PUBLIC_ORIGIN"]:
        fail("first hub install needs the public address: --origin https://fleet.example.com")
    env["FLEET_STATE_DIR"] = str(STATE_DIR)
    env["FLEET_GID"] = str(os.getgid())
    env["FLEET_VERSION"] = version
    env.setdefault("FLEET_NETWORK", "public_proxy")
    if show_hardware is not None:
        env["FLEET_SHOW_HARDWARE"] = "1" if show_hardware else "0"
    env.setdefault("FLEET_SHOW_HARDWARE", "0")
    env_path.write_text("# Written by `fleet install --hub`.\n" + "".join(f"{k}={v}\n" for k, v in env.items()))
    if run("docker", "network", "inspect", env["FLEET_NETWORK"], check=False, capture=True).returncode:
        run("docker", "network", "create", env["FLEET_NETWORK"], capture=True)
    say("building and starting fleet-web")
    run("docker", "compose", "up", "-d", "--build", "--quiet-pull", cwd=repo)
    say(f"fleet-web is up. Point your tunnel's {env['FLEET_PUBLIC_ORIGIN']} at "
        f"http://fleet-web:8080 on the Docker network {env['FLEET_NETWORK']}.")
    say("first time? create your account with: fleet admin create you@example.com")


def cmd_install(args):
    repo = find_repo(args.repo) if args.hub else (Path(args.repo) if args.repo else None)
    version = build_version(repo)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if args.hub:
        HUB_FILE.write_text("This machine is Fleet's hub.\n")
    install_fleetd(version, repo)
    if args.hub:
        install_hub(repo, args.origin, args.show_hardware, version)


def cmd_status(args):
    active = systemctl("is-active", "fleetd", check=False, capture=True).stdout.strip()
    print(f"fleetd     {active}, socket {'answering' if fleetd_healthy() else 'not answering'}")
    if shutil.which("docker"):
        out = run("docker", "ps", "-a", "--filter", "name=^fleet-web$", "--format", "{{.Status}}",
                  check=False, capture=True).stdout.strip()
        if out:
            print(f"fleet-web  {out}")
    print(f"linger     {'on' if linger_on() else 'off (fleetd waits for a login after reboot)'}")


def cmd_uninstall(args):
    systemctl("disable", "--now", "--quiet", "fleetd", check=False)
    UNIT_PATH.unlink(missing_ok=True)
    if args.hub:
        HUB_FILE.unlink(missing_ok=True)
    systemctl("daemon-reload")
    say("fleetd removed")
    if args.hub:
        run("docker", "compose", "down", cwd=find_repo(args.repo))
        say("fleet-web stopped; its accounts stay in the fleet_fleet-web-data volume")


def cmd_admin(args):
    action = {"create": "create-admin", "reset-password": "reset-password"}[args.action]
    sys.exit(run("docker", "exec", "fleet-web", "python", "-m", "fleet_web.cli", action, args.email,
                 check=False).returncode)


def cmd_devices(args):
    items = config.devices()
    if args.action == "add":
        if any(d["name"] == args.name for d in items):
            fail(f"{args.name} is already listed")
        dest = args.ssh or args.name
        probe = run("ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", dest,
                    "python3 --version && test -f ~/.config/systemd/user/fleetd.service && echo has-fleetd",
                    check=False, capture=True)
        if probe.returncode:
            fail(f"`ssh {dest} python3` failed: {(probe.stderr.strip().splitlines() or ['?'])[-1]}\n"
                 "      The hub's fleetd connects with your ssh keys and needs python3 on the device.")
        config.save_devices(items + [{"name": args.name, "ssh": dest}])
        if "has-fleetd" in probe.stdout:
            say(f"added {args.name} (ssh {dest}); it shows on the Devices tab now")
        else:
            say(f"added {args.name} (ssh {dest}). fleetd isn't installed there, so the Devices tab "
                "leaves it out until you run ./install.sh on it")
    elif args.action == "remove":
        if not any(d["name"] == args.name for d in items):
            fail(f"{args.name} isn't listed")
        config.save_devices([d for d in items if d["name"] != args.name])
        say(f"removed {args.name}")
    else:
        for d in items:
            print(f"{d['name']:<16} ssh {d['ssh']}")
        if not items:
            say(f"no other devices yet; add one with: fleet devices add NAME [--ssh DEST]")


def main():
    p = argparse.ArgumentParser(prog="fleet", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    installed = VERSION_FILE.read_text().strip() if VERSION_FILE.exists() else importlib.metadata.version("fleet")
    p.add_argument("--version", action="version", version=f"fleet {installed}")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("install", help="install or update fleetd (and fleet-web with --hub)")
    i.add_argument("--hub", action="store_true", help="also run fleet-web on this machine")
    i.add_argument("--repo", help="the Fleet checkout (default: found from the current directory)")
    i.add_argument("--origin", help="public address of fleet-web, e.g. https://fleet.example.com")
    i.add_argument("--show-hardware", action=argparse.BooleanOptionalAction, default=None,
                   help="show CPU, memory, disk and GPUs on the Devices tab (remembered; default off)")
    i.set_defaults(func=cmd_install)
    sub.add_parser("status", help="show what's running").set_defaults(func=cmd_status)
    u = sub.add_parser("uninstall", help="remove fleetd (and fleet-web with --hub)")
    u.add_argument("--hub", action="store_true")
    u.add_argument("--repo")
    u.set_defaults(func=cmd_uninstall)
    a = sub.add_parser("admin", help="manage fleet-web accounts on the hub")
    a.add_argument("action", choices=["create", "reset-password"])
    a.add_argument("email")
    a.set_defaults(func=cmd_admin)
    d = sub.add_parser("devices", help="list, add or remove devices")
    d.add_argument("action", nargs="?", choices=["add", "remove"])
    d.add_argument("name", nargs="?")
    d.add_argument("--ssh", help="ssh destination, if not the same as NAME")
    d.set_defaults(func=cmd_devices)
    args = p.parse_args()
    if args.cmd == "devices" and args.action and not args.name:
        p.error(f"fleet devices {args.action} needs a NAME")
    try:
        args.func(args)
    except subprocess.CalledProcessError as e:
        fail(f"{' '.join(map(str, e.cmd))} failed (exit {e.returncode})")


if __name__ == "__main__":
    main()
