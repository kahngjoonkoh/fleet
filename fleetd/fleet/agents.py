"""Agents: long-running programs such as Claude Code or a shell, each in its own tmux server inside
its own systemd user unit, so they outlive fleetd, the browser, the ssh link and the hub. Standard
library only: the hub's fleetd runs this file on other machines with `ssh HOST python3 - ACTION ARGS`,
as it does info.py, so a device needs only python3, systemd and tmux.

Agent NAME is the unit fleet-agent@NAME.service, plus these files in ~/.local/state/fleet/agents/:
  NAME.json   what it is: type, folder, start and resume commands, created, start at boot
  NAME.sh     what the unit runs: cd to the folder, then the start command, or after the first start
              the resume command, so a restart or a reboot picks the conversation up again
  NAME.sock   its tmux server's socket
  NAME.asleep there while Fleet has put the agent to sleep; opening it wakes it

An agent that's running, or asleep, is enabled, so a reboot brings it back; one you stop stays stopped.
Asleep means stopped to free its memory: only agents whose conversation resumes are put to sleep,
never pinned ones, and the hub's fleet-web decides when (after a quiet spell set in Settings).
"""

import base64
import glob
import json
import os
import pwd
import re
import socket
import shlex
import shutil
import subprocess
import sys
import time
import uuid

STATE = os.environ.get("FLEET_STATE_DIR", os.path.expanduser("~/.local/state/fleet"))
DIR = os.path.join(STATE, "agents")
UNIT_PATH = os.path.expanduser("~/.config/systemd/user/fleet-agent@.service")
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# What can run as an agent. start and resume are shell commands, run through the user's interactive
# login shell so PATH matches a terminal's; {id} is a fresh UUID per agent. Shell and custom are always offered.
# "lazy": the resume command brings the conversation back, so the agent may be put to sleep when idle.
TYPES = {
    # {rc} becomes --remote-control NAME when the agent has Remote Control on (see _script).
    "claude": {"label": "Claude Code", "bin": "claude", "start": "claude --session-id {id}{rc}", "lazy": True, "rc": True,
               # Resume the agent's own conversation; one never written to (no message yet) starts over.
               "resume": 'if ls "${{CLAUDE_CONFIG_DIR:-$HOME/.claude}}"/projects/*/{id}.jsonl >/dev/null 2>&1; '
                         "then exec claude --resume {id}{rc}; else exec claude --session-id {id}{rc}; fi"},
    "codex": {"label": "Codex", "bin": "codex", "start": "codex", "resume": "codex resume --last", "lazy": True},
    # --session-id creates the session the first time and reopens it after.
    "pi": {"label": "Pi", "bin": "pi", "start": "pi --session-id {id}", "resume": "pi --session-id {id}", "lazy": True},
    "shell": {"label": "Shell"},
    "custom": {"label": "Custom command"},
}

UNIT = """\
# Written by Fleet for its agents; Fleet rewrites it when it changes.
[Unit]
Description=Fleet agent %i

[Service]
Type=forking
# tmux moves each pane into a scope of its own when it can reach the user's systemd, which would put
# the agent outside this unit: its memory uncounted and its limits not applied. Without the bus
# address it leaves panes here; tmux.conf gives them back the address.
UnsetEnvironment=DBUS_SESSION_BUS_ADDRESS XDG_RUNTIME_DIR
ExecStart={tmux} -u -S {dir}/%i.sock -f {dir}/tmux.conf new-session -d -s %i -x 200 -y 50 {dir}/%i.sh
# When memory runs out, the kernel picks an agent before fleetd, sshd or your desktop.
OOMScoreAdjust=500
# A crashed agent stays on screen (remain-on-exit) instead of restarting, so you can see why.
Restart=no

[Install]
WantedBy=default.target
"""

TMUX_CONF = """\
# Written by Fleet for agents' tmux servers; ~/.tmux.conf isn't read for them.
set -g remain-on-exit on
set -g history-limit 10000
set -g status off
# Scrolling with the wheel or a finger moves through tmux's history instead of sending arrow keys.
set -g mouse on
set -g escape-time 10
set -g default-terminal tmux-256color
set -ga terminal-features ",xterm-256color:RGB"
set -g set-clipboard on
set-environment -g COLORTERM truecolor
set-environment -g XDG_RUNTIME_DIR {runtime}
set-environment -g DBUS_SESSION_BUS_ADDRESS unix:path={runtime}/bus
"""


class Problem(Exception):
    """Something to tell the person who asked, e.g. a name that's taken."""


def _env():
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _run(*cmd, timeout=15):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=_env())
        return p.returncode, p.stdout, p.stderr.strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


def _systemctl(*args):
    code, out, err = _run("systemctl", "--user", *args, timeout=30)
    if code:
        raise Problem(f"systemctl {' '.join(args)}: {err or f'exit {code}'}")
    return out


def _shell():
    return pwd.getpwuid(os.getuid()).pw_shell or "/bin/sh"


def _home(path):
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + "/") else path


def _write(path, text, mode=0o600):
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def _setup():
    """The agents directory, tmux.conf and the unit template, rewritten whenever Fleet changes them."""
    os.makedirs(DIR, mode=0o700, exist_ok=True)
    os.chmod(DIR, 0o700)  # fleet-web can read STATE, but not what's in here
    tmux = shutil.which("tmux")
    if not tmux:
        raise Problem("tmux isn't installed on this machine")
    _write(os.path.join(DIR, "tmux.conf"), TMUX_CONF.format(runtime=f"/run/user/{os.getuid()}"))
    unit = UNIT.format(tmux=tmux, dir=DIR)
    try:
        with open(UNIT_PATH) as f:
            same = f.read() == unit
    except OSError:
        same = False
    if not same:
        os.makedirs(os.path.dirname(UNIT_PATH), exist_ok=True)
        _write(UNIT_PATH, unit, 0o644)
        _systemctl("daemon-reload")


def _records():
    try:
        names = sorted(f[:-5] for f in os.listdir(DIR) if f.endswith(".json"))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        try:
            with open(os.path.join(DIR, name + ".json")) as f:
                out.append(json.load(f))
        except (OSError, ValueError):
            continue
    return out


def _units(names):
    """systemd's view of each agent's unit: state, main PID (the tmux server), memory, start time."""
    if not names:
        return {}
    props = "Id,ActiveState,MainPID,MemoryCurrent,ActiveEnterTimestampMonotonic,UnitFileState"
    code, out, _ = _run("systemctl", "--user", "show", "-p", props, *(f"fleet-agent@{n}.service" for n in names))
    units = {}
    for block in out.strip().split("\n\n"):
        u = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        if u.get("Id", "").startswith("fleet-agent@"):
            units[u["Id"][len("fleet-agent@"):-len(".service")]] = u
    return units


def _boot_time():
    with open("/proc/uptime") as f:
        return time.time() - float(f.read().split()[0])


def _pane(name, pid):
    """Asks the agent's own tmux server about its pane, with the binary that server runs, so a tmux
    upgrade on disk never leaves a client that can't talk to an older server."""
    code, out, _ = _run(f"/proc/{pid}/exe", "-S", os.path.join(DIR, name + ".sock"), "display", "-p", "-t", name,
                        "#{pane_dead}|#{pane_dead_status}|#{window_activity}|#{pane_current_command}|#{session_attached}|#{session_activity}",
                        timeout=5)
    parts = out.strip().split("|")
    if code or len(parts) != 6:
        return None
    dead, status, output, command, attached, typed = parts
    times = [int(t) for t in (output, typed) if t.isdigit()]
    return {"dead": dead == "1", "exit_code": int(status) if status.lstrip("-").isdigit() else None,
            # The last output or keypress, whichever is later: what counts as activity for sleeping.
            "last_active": max(times) if times else None, "command": command,
            "viewers": int(attached) if attached.isdigit() else 0}


def _int(s):
    return int(s) if s and s.isdigit() else None


def list_agents(_args=None):
    records = _records()
    units = _units([r["name"] for r in records])
    boot = _boot_time()
    agents = []
    for r in records:
        u = units.get(r["name"], {})
        pid = _int(u.get("MainPID"))
        active = u.get("ActiveState") == "active" and pid
        pane = _pane(r["name"], pid) if active else None
        if not active:
            state = ("asleep" if os.path.exists(_path(r["name"], ".asleep"))
                     else "failed" if u.get("ActiveState") == "failed" else "stopped")
        elif pane is None:
            state = "running"  # tmux didn't answer; the unit is up regardless
        else:
            state = "exited" if pane["dead"] else "running"
        started = _int(u.get("ActiveEnterTimestampMonotonic"))
        memory = _int(u.get("MemoryCurrent"))
        agents.append({
            "name": r["name"], "type": r["type"], "label": TYPES.get(r["type"], {}).get("label", r["type"]),
            "folder": _home(r["folder"]), "created": r["created"], "pinned": r.get("pinned", False),
            "remote_control": r.get("remote_control", False), "rc_capable": TYPES.get(r["type"], {}).get("rc", False),
            "lazy": TYPES.get(r["type"], {}).get("lazy", False),
            "state": state, "pane": pane, "memory": memory if memory and memory < 2**63 else None,
            "started": int(boot + started / 1e6) if active and started else None,
        })
    return {"agents": agents, "tmux": _other_tmux(), "now": int(time.time())}


def _other_tmux():
    """Sessions on the default tmux server, which Fleet didn't start and leaves alone: Attach only."""
    if not shutil.which("tmux"):
        return []
    code, out, _ = _run("tmux", "list-sessions", "-F", "#{session_name}|#{session_created}|#{session_attached}", timeout=5)
    sessions = []
    for line in out.splitlines() if not code else []:
        name, created, attached = (line.split("|") + ["", ""])[:3]
        sessions.append({"name": name, "created": _int(created), "viewers": _int(attached) or 0})
    return sessions


def _which(names):
    """Which of these programs the user's interactive login shell finds, as a terminal would."""
    script = "".join(f"command -v {shlex.quote(n)} >/dev/null 2>&1 && echo {shlex.quote(n)}; " for n in names)
    code, out, _ = _run(_shell(), "-l", "-i", "-c", script, timeout=10)
    return set(out.split())


def _folders(limit=300):
    """Git checkouts under the home folder, three levels down at most, newest first: the folder field's suggestions."""
    home = os.path.expanduser("~")
    skip = {"node_modules", "venv", "snap", "go", "vendor", "site-packages"}
    found, stack = [], [(home, 0)]
    while stack and len(found) < limit:
        path, depth = stack.pop()
        try:
            entries = list(os.scandir(path))
        except OSError:
            continue
        if path != home and any(e.name == ".git" for e in entries):
            found.append(path)
            continue
        if depth < 3:
            stack += [(e.path, depth + 1) for e in entries
                      if e.is_dir(follow_symlinks=False) and not e.name.startswith(".") and e.name not in skip]

    def mtime(p):
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0

    return [_home(p) for p in sorted(found, key=mtime, reverse=True)]


def options(_args=None):
    """What the New agent form offers on this machine."""
    installed = _which([t["bin"] for t in TYPES.values() if "bin" in t])
    user = pwd.getpwuid(os.getuid()).pw_name
    code, out, _ = _run("loginctl", "show-user", user, "-p", "Linger", "--value", timeout=5)
    return {
        "types": [{"id": k, "label": t["label"]} for k, t in TYPES.items() if "bin" not in t or t["bin"] in installed],
        "folders": _folders(),
        "tmux": bool(shutil.which("tmux")),
        "linger": out.strip() == "yes",
        "user": user,
        "taken": [r["name"] for r in _records()],
    }


def _script(record):
    """The agent's start script; rewritten on every start, so changed settings take effect then."""
    shell = _shell()
    # Claude Code and pi show the agent's name as the session's name; Claude Code also takes Remote Control.
    # {rc} marks where these go; agents made before it have it added after the session id.
    name = shlex.quote(record["name"])
    flags = (f" --name {name}" if record["type"] in ("claude", "pi") else "") + \
        (f" --remote-control {name}" if record.get("remote_control") and record["type"] == "claude" else "")
    with_rc = lambda cmd: cmd if "{rc}" in cmd or record["type"] not in ("claude", "pi") else \
        re.sub(r"(--(?:resume|session-id) [0-9a-f-]{36})", r"\1{rc}", cmd)
    record = record | {k: with_rc(record[k]).replace("{rc}", flags) for k in ("start", "resume") if record.get(k)}
    # Interactive too: tools such as nvm are often set up only in ~/.bashrc, which skips non-interactive shells.
    run = lambda cmd: f"exec {shlex.quote(shell)} -l" if cmd is None else f"exec {shlex.quote(shell)} -l -i -c {shlex.quote(cmd)}"
    folder, marker = shlex.quote(record["folder"]), shlex.quote(os.path.join(DIR, record["name"] + ".started"))
    who = shlex.quote(record.get("fleet_agent") or record["name"])
    return (f"#!/bin/sh\n# Written by Fleet for agent {record['name']}.\n"
            # Who this agent is, as the hub names it: the brain's /me/$FLEET_AGENT reads its part.
            f"export FLEET_AGENT={who}\n"
            f"cd {folder} || {{ echo \"Fleet: can't open the folder {record['folder']}\"; exit 1; }}\n"
            f"if [ -e {marker} ]; then {run(record['resume'])}; fi\n"
            f": > {marker}\n{run(record['start'])}\n")


def create(args):
    name, kind = args.get("name", ""), args.get("type", "")
    if not NAME.match(name):
        raise Problem("Names are 1-32 letters, digits, dashes or underscores, starting with a letter or digit.")
    if kind not in TYPES:
        raise Problem("Pick an agent type.")
    folder = os.path.abspath(os.path.expanduser((args.get("folder") or "~").strip()))
    if not os.path.isdir(folder):
        raise Problem(f"There's no folder {_home(folder)} on this machine.")
    t = TYPES[kind]
    if kind == "custom":
        command = (args.get("command") or "").strip()
        if not command:
            raise Problem("Enter the command to run.")
        start = resume = command
    elif kind == "shell":
        start = resume = None
    else:
        if t["bin"] not in _which([t["bin"]]):
            raise Problem(f"{t['label']} ({t['bin']}) isn't installed on this machine.")
        aid = str(uuid.uuid4())
        start, resume = t["start"].format(id=aid, rc="{rc}"), t["resume"].format(id=aid, rc="{rc}")
    _setup()
    path = os.path.join(DIR, name + ".json")
    if os.path.exists(path):
        raise Problem(f"There's already an agent called {name} here.")
    record = {"name": name, "type": kind, "folder": folder, "start": start, "resume": resume,
              "created": int(time.time()), "pinned": bool(args.get("pinned")),
              "remote_control": bool(args.get("remote_control")) and t.get("rc", False)}
    if args.get("fleet_agent"):
        record["fleet_agent"] = str(args["fleet_agent"])[:100]
    if "{id}" in t.get("start", ""):
        record["session"] = aid  # the agent's conversation, for the project brain to read
    _write(path, json.dumps(record, indent=2) + "\n")
    _write(os.path.join(DIR, name + ".sh"), _script(record), 0o700)
    try:
        _systemctl("enable", "--now", "--quiet", f"fleet-agent@{name}.service")
    except Problem:
        _remove_files(name)
        _run("systemctl", "--user", "disable", "--quiet", f"fleet-agent@{name}.service")
        raise
    return {"ok": True}


def _path(name, ext):
    return os.path.join(DIR, name + ext)


def _record(args):
    name = args.get("name", "")
    if not NAME.match(name) or not os.path.exists(_path(name, ".json")):
        raise Problem(f"There's no agent called {name} here.")
    return name


def _load(name):
    with open(_path(name, ".json")) as f:
        return json.load(f)


def _unit(name):
    return f"fleet-agent@{name}.service"


def _is_active(name):
    return _run("systemctl", "--user", "is-active", "--quiet", _unit(name))[0] == 0


def _awake(name):
    """Running, and back after a reboot. Restart, because an exited agent's unit is still active."""
    _setup()
    _write(_path(name, ".sh"), _script(_load(name)), 0o700)
    try:
        os.unlink(_path(name, ".asleep"))
    except FileNotFoundError:
        pass
    _systemctl("enable", "--quiet", _unit(name))
    _systemctl("restart", _unit(name))


def start(args):
    _awake(_record(args))
    return {"ok": True}


def stop(args):
    name = _record(args)
    _systemctl("disable", "--now", "--quiet", _unit(name))
    try:
        os.unlink(_path(name, ".asleep"))
    except FileNotFoundError:
        pass
    return {"ok": True}


def sleep(args):
    """Stops an idle agent to free its memory; it stays disabled until opened, so a reboot doesn't wake it."""
    name = _record(args)
    record = _load(name)
    if record.get("pinned") or not TYPES.get(record["type"], {}).get("lazy"):
        raise Problem(f"{name} is pinned or can't resume, so it doesn't sleep.")
    _write(_path(name, ".asleep"), "")
    _systemctl("disable", "--now", "--quiet", _unit(name))
    return {"ok": True}


def wake(args):
    """Before opening an agent: starts it if it's asleep. A stopped agent stays stopped."""
    name = _record(args)
    woke = os.path.exists(_path(name, ".asleep")) and not _is_active(name)
    if woke:
        _awake(name)
    return {"ok": True, "woke": woke}


def settings(args):
    """Changes an agent's Remote Control; it takes effect the next time the agent starts."""
    name = _record(args)
    record = _load(name)
    if "remote_control" in args:
        record["remote_control"] = bool(args["remote_control"]) and TYPES.get(record["type"], {}).get("rc", False)
    _write(_path(name, ".json"), json.dumps(record, indent=2) + "\n")
    _write(_path(name, ".sh"), _script(record), 0o700)
    return {"ok": True}


def rename(args):
    """Renames an agent. Its unit and tmux server are named after it, so a running agent restarts
    under the new name (Claude Code and Pi pick their conversation back up); an asleep or stopped one
    stays that way."""
    old = _record(args)
    new = args.get("new", "")
    if not NAME.match(new):
        raise Problem("Names are 1-32 letters, digits, dashes or underscores, starting with a letter or digit.")
    if os.path.exists(_path(new, ".json")):
        raise Problem(f"There's already an agent called {new} here.")
    running, asleep = _is_active(old), os.path.exists(_path(old, ".asleep"))
    _systemctl("disable", "--now", "--quiet", _unit(old))
    record = _load(old)
    record["name"] = new
    if args.get("fleet_agent"):
        record["fleet_agent"] = str(args["fleet_agent"])[:100]
    for ext in (".started", ".asleep"):
        if os.path.exists(_path(old, ext)):
            os.replace(_path(old, ext), _path(new, ext))
    _write(_path(new, ".json"), json.dumps(record, indent=2) + "\n")
    _write(_path(new, ".sh"), _script(record), 0o700)
    for ext in (".json", ".sh", ".sock"):
        try:
            os.unlink(_path(old, ext))
        except FileNotFoundError:
            pass
    if running:
        _awake(new)
    elif asleep:
        _systemctl("disable", "--quiet", _unit(new))
    return {"ok": True, "restarted": running}


def pin(args):
    """Pinned agents never sleep; pinning one that's asleep wakes it."""
    name = _record(args)
    record = _load(name)
    record["pinned"] = bool(args.get("pinned"))
    _write(_path(name, ".json"), json.dumps(record, indent=2) + "\n")
    if record["pinned"]:
        wake({"name": name})
    return {"ok": True}


def _remove_files(name):
    for ext in (".json", ".sh", ".started", ".sock", ".asleep"):
        try:
            os.unlink(_path(name, ext))
        except FileNotFoundError:
            pass


def remove(args):
    name = _record(args)
    _systemctl("disable", "--now", "--quiet", _unit(name))
    _remove_files(name)
    return {"ok": True}


# --- for the project brain on the hub --------------------------------------------

def paths(_args=None):
    return {"state": STATE, "home": os.path.expanduser("~"), "host": socket.gethostname()}


def folder(args):
    """Resolves a folder as the New project form gave it: {"folder": "~/dev/x", "exists": bool}."""
    path = os.path.abspath(os.path.expanduser((args.get("folder") or "~").strip()))
    return {"folder": _home(path), "path": path, "exists": os.path.isdir(path)}


def _conversation(record):
    """The agent's own conversation file, or None before its first message is saved."""
    sid = record.get("session") or next(iter(UUID.findall(record.get("start") or "")), None)
    if not sid:
        return None
    if record["type"] == "claude":
        base = os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude"))
        found = glob.glob(os.path.join(base, "projects", "*", sid + ".jsonl"))
    elif record["type"] == "pi":
        base = os.environ.get("PI_CODING_AGENT_SESSION_DIR", os.path.expanduser("~/.pi/agent/sessions"))
        found = glob.glob(os.path.join(base, "*", f"*_{sid}.jsonl"))
    else:
        return None
    return max(found, key=os.path.getmtime) if found else None


def _clip(text, n):
    text = str(text).strip()
    return text if len(text) <= n else text[:n] + f" [... {len(text) - n} more characters]"


def _render(entry):
    """One line of a Claude Code or pi conversation file as plain text; "" for bookkeeping lines."""
    if entry.get("type") == "message":  # pi
        message, who = entry.get("message") or {}, None
        role = message.get("role")
    elif entry.get("type") in ("user", "assistant") and not entry.get("isMeta"):  # Claude Code
        message, role = entry.get("message") or {}, entry["type"]
    else:
        return ""
    if role not in ("user", "assistant", "toolResult"):
        return ""
    content = message.get("content")
    blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content or []
    out = []
    for b in blocks:
        kind = b.get("type", "")
        if kind == "text" and b.get("text", "").strip():
            if entry.get("isCompactSummary"):  # Claude Code's summary of everything before it: keep it whole
                out.append(f"SUMMARY OF THE SESSION SO FAR: {_clip(b['text'], 30000)}")
                continue
            label = "USER" if role == "user" else "RESULT" if role == "toolResult" else "AGENT"
            out.append(f"{label}: {_clip(b['text'], 3000 if label != 'RESULT' else 400)}")
        elif kind in ("tool_use", "toolCall"):
            args = b.get("input", b.get("arguments", {}))
            out.append(f"AGENT ran {b.get('name')}: {_clip(json.dumps(args, ensure_ascii=False), 400)}")
        elif kind == "tool_result":
            c = b.get("content")
            text = c if isinstance(c, str) else " ".join(x.get("text", "") for x in c or [] if isinstance(x, dict))
            out.append(f"RESULT: {_clip(text, 400)}")
    return "\n".join(out)


def _session_roots():
    return [os.path.realpath(os.path.join(os.environ.get("CLAUDE_CONFIG_DIR", os.path.expanduser("~/.claude")), "projects")),
            os.path.realpath(os.environ.get("PI_CODING_AGENT_SESSION_DIR", os.path.expanduser("~/.pi/agent/sessions")))]


def transcript(args):
    """A conversation from byte `offset`, as text of at most `limit` characters, cut at a line, with
    the offset to continue from. Fleet's brain writer reads conversations this way: an agent's own
    ({"name"}), or any Claude Code or pi session file ({"path"}, e.g. for /fleet-save)."""
    if args.get("path"):
        path = os.path.realpath(os.path.expanduser(args["path"]))
        if not path.endswith(".jsonl") or not any(path.startswith(root + os.sep) for root in _session_roots()):
            raise Problem("Only Claude Code and pi session files can be read.")
        if not os.path.isfile(path):
            raise Problem(f"There's no session file {_home(path)}.")
        record = {"type": "pi" if path.startswith(_session_roots()[1]) else "claude",
                  "folder": os.path.abspath(os.path.expanduser(args.get("folder") or "~"))}
    else:
        record = _load(_record(args))
        path = _conversation(record)
    if not path:
        return {"text": "", "offset": 0, "size": 0, "done": True, "type": record["type"]}
    size = os.path.getsize(path)
    offset = int(args.get("offset") or 0)
    if offset > size:  # a different or rewritten file: start over
        offset = 0
    limit, parts, used = int(args.get("limit") or 60000), [], 0
    with open(path, "rb") as f:
        f.seek(offset)
        for line in f:
            if not line.endswith(b"\n"):
                break  # still being written
            try:
                text = _render(json.loads(line))
            except ValueError:
                text = ""
            if text and used + len(text) > limit and parts:
                break
            offset += len(line)
            if text:
                parts.append(text)
                used += len(text) + 1
    return {"text": "\n".join(parts), "offset": offset, "size": size, "done": offset >= size,
            "type": record["type"], "folder": _home(record["folder"])}


BRAIN_START, BRAIN_END = "<!-- fleet:brain -->", "<!-- /fleet:brain -->"


def instructions(args):
    """Puts Fleet's note about the project brain into AGENTS.md (which Codex, pi and Claude Code read)
    between markers, and makes CLAUDE.md import it, since Claude Code otherwise reads AGENTS.md only
    when there's no CLAUDE.md. Anything else in either file is left as it is."""
    path = os.path.abspath(os.path.expanduser(args["folder"]))
    if not os.path.isdir(path):
        raise Problem(f"There's no folder {_home(path)} on this machine.")
    block = f"{BRAIN_START}\n{args['block'].strip()}\n{BRAIN_END}"
    changed = []
    agents_md = os.path.join(path, "AGENTS.md")
    try:
        with open(agents_md) as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    if BRAIN_START in old and BRAIN_END in old:
        new = old[:old.index(BRAIN_START)] + block + old[old.index(BRAIN_END) + len(BRAIN_END):]
    else:
        new = (old.rstrip() + "\n\n" if old.strip() else "") + block + "\n"
    if new != old:
        _write(agents_md, new, 0o644)
        changed.append("AGENTS.md")
    claude_md = os.path.join(path, "CLAUDE.md")
    try:
        with open(claude_md) as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    if not re.search(r"(?m)^@AGENTS\.md\s*$", old):
        _write(claude_md, "@AGENTS.md\n" + ("\n" + old if old else ""), 0o644)
        changed.append("CLAUDE.md")
    return {"ok": True, "changed": changed}


def attach_command(name=None, tmux=None):
    """A shell command that joins an agent's tmux session (or, with tmux=, a session on the default
    server), for the Terminal tab to run here or over ssh. It runs the agent's tmux server's own binary."""
    if tmux is not None:
        return f"exec tmux -u attach -t {shlex.quote('=' + tmux)}"
    unit = shlex.quote(f"fleet-agent@{name}.service")
    sock = shlex.quote(name + ".sock")
    return (f'p=$(systemctl --user show -p MainPID --value {unit} 2>/dev/null); '
            f'if [ -z "$p" ] || [ "$p" = 0 ]; then echo "Agent {name} isn\'t running. Start it from the Agents tab."; exit 1; fi; '
            f'exec /proc/$p/exe -u -S "${{FLEET_STATE_DIR:-$HOME/.local/state/fleet}}/agents/"{sock} attach -t {shlex.quote(name)}')


ACTIONS = {"list": list_agents, "options": options, "create": create, "start": start, "stop": stop, "remove": remove,
           "sleep": sleep, "wake": wake, "pin": pin, "settings": settings, "rename": rename, "paths": paths, "folder": folder, "transcript": transcript,
           "instructions": instructions}


def run_action(action, args):
    try:
        return ACTIONS[action](args or {})
    except Problem as e:
        return {"error": str(e)}


if __name__ == "__main__":
    # ssh HOST python3 - ACTION [base64 JSON]: the arguments travel as base64 so no shell quoting is involved.
    arg = json.loads(base64.b64decode(sys.argv[2])) if len(sys.argv) > 2 else {}
    print(json.dumps(run_action(sys.argv[1], arg)))
