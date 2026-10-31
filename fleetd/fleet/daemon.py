"""fleetd: the per-host Fleet daemon. It listens only on a unix socket that fleet-web reaches, and
does the host-side work as this user: device checks, agents, and the Terminal tab's shells. Other
machines are reached over ssh, running info.py or agents.py there."""

import asyncio
import base64
import fcntl
import json
import os
import pty
import pwd
import re
import signal
import socket
import struct
import termios
import time
from pathlib import Path

import aiohttp
from aiohttp import WSMsgType, web

from . import GITHUB_REPO, HUB_FILE, SOCKET, STATE_DIR, agents, brain, config, info

HOST = socket.gethostname()
# The read-only brain socket agents use; on other devices the same path is an ssh forward to this one.
BRAIN_SOCKET = STATE_DIR / "brain.sock"
INFO_SCRIPT = Path(info.__file__).read_bytes()
AGENTS_SCRIPT = Path(agents.__file__).read_bytes()
# ssh connection sharing, kept out of STATE_DIR because fleet-web can see that directory.
SSH_MUX = Path.home() / ".cache/fleet"


async def health(request):
    return web.json_response({"ok": True, "host": HOST})


async def run_remote(device, script, args, timeout=15):
    """Runs a standard-library script on another machine with `ssh DEST python3 - ARGS` and returns its
    JSON output, or {"error": ...} when ssh, python3 or the script fails."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", *ssh_options(), device["ssh"], "python3", "-", *args,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(script), timeout)
        except TimeoutError:
            proc.kill()
            return {"error": f"no answer within {timeout} seconds"}
        if proc.returncode:
            lines = err.decode(errors="replace").strip().splitlines()
            return {"error": lines[-1] if lines else f"ssh exited {proc.returncode}"}
        return {"result": json.loads(out)}
    except (OSError, ValueError) as e:
        return {"error": str(e)}


async def probe(device, hardware):
    """Runs info.py on another machine over ssh; it needs only python3 there, not Fleet."""
    entry = {"name": device["name"], "ssh": device["ssh"], "local": False}
    r = await run_remote(device, INFO_SCRIPT, ["--hardware"] if hardware else [])
    if "error" in r:
        return entry | {"online": False, "error": r["error"]}
    return entry | {"online": True, "info": r["result"]}


_compare_cache = {}  # commit -> (expires, result)


async def update_status(version):
    """How a version like 0.1.0+bcb752b compares with main on GitHub:
    {"available": bool, "behind_by": n}, or None if unknown (no commit, not pushed, GitHub unreachable)."""
    m = re.search(r"\+([0-9a-f]{7,40})", version or "")
    if not m:
        return None
    commit = m.group(1)
    hit = _compare_cache.get(commit)
    if hit and hit[0] > time.time():
        return hit[1]
    url = f"https://api.github.com/repos/{GITHUB_REPO}/compare/{commit}...main"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as s:
            async with s.get(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "fleetd"}) as r:
                if r.status == 404:  # a commit GitHub doesn't have, e.g. not pushed yet
                    result = None
                elif r.status == 200:
                    j = await r.json()
                    # base=installed, head=main: main "ahead" (or "diverged") means newer commits exist.
                    result = {"available": j["ahead_by"] > 0, "behind_by": j["ahead_by"]}
                else:  # rate limited or GitHub trouble: try again in a few minutes
                    _compare_cache[commit] = (time.time() + 300, None)
                    return None
    except (aiohttp.ClientError, TimeoutError, ValueError, KeyError):
        return None
    _compare_cache[commit] = (time.time() + 3600, result)
    return result


async def update(request):
    return web.json_response({"update": await update_status(request.query.get("version"))})


async def devices(request):
    """This machine plus every machine in config.toml, checked in parallel. ?hardware=1 adds
    OS, CPU, memory, disk and GPU facts."""
    hardware = request.query.get("hardware") == "1"
    local = asyncio.to_thread(info.collect, hardware)
    remote = [probe(d, hardware) for d in config.devices()]
    mine, *others = await asyncio.gather(local, *remote)
    here = {"name": HOST, "ssh": None, "local": True, "online": True, "info": mine}
    found = [here, *others]
    statuses = await asyncio.gather(*(update_status(d.get("info", {}).get("fleetd_version")) for d in found))
    for d, status in zip(found, statuses):
        d["update"] = status
    return web.json_response({"devices": found, "checked_at": int(time.time())})


def find_device(name):
    """None for this machine, else its config.toml entry; 404 for a name that's neither."""
    if not name or name == HOST:
        return None
    device = next((d for d in config.devices() if d["name"] == name), None)
    if device is None:
        raise web.HTTPNotFound(text=f"no device named {name}")
    return device


async def agents_call(device, action, args=None, timeout=20):
    """Runs an agents.py action here, or on another machine over ssh. Returns its result, or
    {"error": ...}, with "offline": True when the machine couldn't be reached at all."""
    if device is None:
        return await asyncio.to_thread(agents.run_action, action, args or {})
    arg = base64.b64encode(json.dumps(args or {}).encode()).decode()
    r = await run_remote(device, AGENTS_SCRIPT, [action, arg], timeout)
    return r["result"] if "result" in r else {"error": r["error"], "offline": True}


async def everywhere(action):
    """An agents.py action on this machine and every configured device, in parallel."""
    targets = [None, *config.devices()]
    results = await asyncio.gather(*(agents_call(d, action) for d in targets))
    return [{"name": d["name"] if d else HOST, "local": d is None} | r for d, r in zip(targets, results)]


async def agents_list(request):
    devices = await everywhere("list")
    if HUB_FILE.exists():  # each agent's home project, which the brain decides
        for d in devices:
            for a in d.get("agents", []):
                a["project"] = brain.home_of(f"{d['name']}/{a['name']}")
    return web.json_response({"devices": devices, "checked_at": int(time.time())})


_options = (0, None)  # the New agent form's per-device types and folders, briefly remembered


async def agents_options(request):
    """Which agent types and folders each device offers. Scanning folders and asking a login shell
    which programs exist takes a moment, so the answer is kept for a minute."""
    global _options
    if _options[0] < time.time() or request.query.get("fresh"):
        _options = (time.time() + 60, await everywhere("options"))
    return web.json_response({"devices": _options[1]})


async def agents_action(request):
    """POST /agents/{create,start,stop,remove,sleep,wake,pin,settings,rename} with a JSON body naming the device and the agent."""
    action = request.match_info["action"]
    if action not in ("create", "start", "stop", "remove", "sleep", "wake", "pin", "settings", "rename"):
        raise web.HTTPNotFound()
    body = await request.json()
    name = body.pop("device", None) or HOST
    device = find_device(name)
    here = device["name"] if device else HOST
    if action in ("create", "rename"):  # agents know who they are, to read their part of the brain
        body["fleet_agent"] = f"{here}/{body.get('new') or body.get('name', '')}"
    result = await agents_call(device, action, body, timeout=60)
    if action == "rename" and "error" not in result and HUB_FILE.exists():
        brain.rename_agent(f"{here}/{body.get('name')}", f"{here}/{body.get('new')}")
    if action == "remove" and "error" not in result and HUB_FILE.exists():
        brain.forget_agent(f"{here}/{body.get('name', '')}")
    return web.json_response(result)


# --- projects and the brain (the hub) -------------------------------------------------------

def problem(e):
    return web.json_response({"error": str(e)})


async def projects_list(request):
    return web.json_response({"projects": [p | brain.summary(p["name"]) for p in brain.projects()]})


async def project_detail(request):
    name = request.match_info["name"]
    try:
        p = brain.project(name)
    except brain.Problem as e:
        raise web.HTTPNotFound(text=str(e))
    return web.json_response({"project": p | brain.summary(name) | {"agents": brain.members(name)},
                              "overview": (brain.note(f"overview/{name}") or {}).get("body", ""),
                              "notes": brain.notes(name), "graph": brain.graph(name)})


async def brain_note(request):
    note_id = request.query.get("id", "")
    n = brain.note(note_id)
    if n is None:
        raise web.HTTPNotFound(text="no such note")
    near = brain.neighbourhood(note_id, 1)
    return web.json_response({"id": note_id, "note": n, "backlinks": brain.backlinks(note_id),
                              "titles": brain.titles(near), "projects": [p["name"] for p in brain.projects()]})


async def brain_overview(request):
    """The Projects tab: the all-projects graph and the rules for every agent."""
    return web.json_response({"graph": brain.graph_all(), "rules": brain.notes(scope="global")})


async def brain_search(request):
    try:
        return web.json_response({"results": brain.search(request.query.get("q", ""), request.query.get("project") or None, 50)})
    except brain.Problem as e:
        return problem(e)


async def write_instructions(device_name, folder):
    """Fleet's brain section in the folder's AGENTS.md (and @AGENTS.md in its CLAUDE.md)."""
    return await agents_call(find_device(device_name), "instructions", {"folder": folder, "block": brain.instructions_block()})


async def instruct_members(name):
    """Writes the brain section into every folder the project's agents work in."""
    changed = set()
    for key in brain.members(name):
        device_name, _, agent = key.partition("/")
        folder = await agent_folder(device_name, agent, fresh=True)
        if folder:
            result = await write_instructions(device_name, folder)
            if "error" in result:
                return result
            changed |= {f"{key}: {f}" for f in result["changed"]}
    return {"ok": True, "changed": sorted(changed)}


async def projects_action(request):
    """POST /projects/{create,delete,instructions} with a JSON body."""
    action, body = request.match_info["action"], await request.json()
    try:
        if action == "create":
            brain.create(body.get("name", ""))
            return web.json_response({"ok": True})
        if action == "delete":
            brain.delete(body.get("name", ""))
            return web.json_response({"ok": True})
        if action == "instructions":
            brain.project(body.get("name", ""))
            return web.json_response(await instruct_members(body["name"]))
    except brain.Problem as e:
        return problem(e)
    raise web.HTTPNotFound()


async def brain_action(request):
    """POST /brain/{digest,home,edit,delete,rule} with a JSON body."""
    action, body = request.match_info["action"], await request.json()
    try:
        if action == "digest":
            return web.json_response(await digest(body))
        if action == "writer":
            brain.set_writer(body)
        if action == "home":  # put an agent in a project (or none), and tell its folder about the brain
            brain.set_home(f"{body['device']}/{body['agent']}", body.get("project") or "")
            if body.get("project"):
                folder = await agent_folder(body["device"], body["agent"], fresh=True)
                if folder:
                    result = await write_instructions(body["device"], folder)
                    if "error" in result:
                        return web.json_response(result)
        elif action == "edit":
            brain.edit(body["id"], body.get("title", ""), body.get("body", ""), body.get("status", ""),
                       "global" if body.get("scope") == "global" else "project", body.get("projects", []))
        elif action == "delete":
            brain.delete_note(body["id"])
        elif action == "rule":
            return web.json_response({"ok": True, "id": brain.add_rule(body.get("title", ""), body.get("body", ""))})
        else:
            raise web.HTTPNotFound()
        return web.json_response({"ok": True})
    except brain.Problem as e:
        return problem(e)


MIN_SLICE = 600  # characters of new conversation worth a writer call; less waits for more (unless forced)


async def digest(body):
    """Reads what a session has said since last time and has the writer fold it into the brain, a
    slice of up to 60,000 characters at a time. The session is an agent's ({"device", "agent"}) or
    any Claude Code or pi session file on a device ({"device", "path", "folder"}, from /fleet-save).
    Returns what it wrote, and whether there's more to read."""
    device_name = body["device"]
    device = find_device(device_name)
    if body.get("path"):
        key, source = f"{device_name}/file:{body['path']}", {"path": body["path"], "folder": body.get("folder")}
        who = f"{device_name} session in {body.get('folder') or '?'}"
    else:
        key = who = f"{device_name}/{body['agent']}"
        source = {"name": body["agent"]}
    chunks, written, done = 0, [], True
    async with brain.lock():
        offset = brain.read_offset(key)
        while chunks < 6:
            t = await agents_call(device, "transcript", source | {"offset": offset, "limit": body.get("limit") or 60000})
            if "error" in t:
                return t
            done = t["done"]
            if not t["text"].strip() or (len(t["text"]) < MIN_SLICE and t["done"] and not body.get("force")):
                if not t["text"].strip():
                    brain.save_offset(key, t["offset"])
                break
            home = (body.get("project") or (brain.home_of(key) if body.get("agent")
                                             else await project_for_folder(device_name, t.get("folder") or "")))
            prompt, shown = brain.writer_input(who, home, t["text"])
            data = await brain.write(prompt, body.get("writer") or brain.writer())
            written += [i for i in brain.apply(data, f"{who} {time.strftime('%Y-%m-%d')}", home, shown) if i not in written]
            offset = t["offset"]
            brain.save_offset(key, offset)
            chunks += 1
            if t["done"]:
                break
    return {"ok": True, "chunks": chunks, "notes": len(written), "written": written, "done": done}


_hosts = {}  # hostname -> device name, learned from each device's agents.py "paths"


async def device_for_host(hostname):
    """The device name the hub uses for a machine that knows itself as `hostname`."""
    if hostname in (HOST, "", None):
        return HOST
    if hostname not in _hosts:
        for d in config.devices():
            where = await agents_call(d, "paths")
            if "host" in where:
                _hosts[where["host"]] = d["name"]
    if hostname not in _hosts:
        raise brain.Problem(f"No device in Fleet is the machine {hostname}; add it with `fleet devices add`.")
    return _hosts[hostname]


async def brain_writer(request):
    if request.method == "POST":
        brain.set_writer(await request.json())
    w = brain.writer()
    key = w.pop("key")
    return web.json_response(w | {"key_hint": f"…{key[-4:]}" if len(key) > 8 else ("set" if key else "")})


async def project_for_folder(device_name, folder):
    """For a session Fleet didn't start: the project of an agent working in that folder (or one
    containing it) on that device, the deepest folder winning; else None."""
    await agent_folder(device_name, "", fresh=True)  # refreshes every agent's folder on the device
    found = [(f, brain.home_of(key)) for key, (_, f) in _folders.items() if key.startswith(device_name + "/") and f
             and (folder == f or folder.startswith(f.rstrip("/") + "/"))]
    found = [(f, p) for f, p in found if p]
    return max(found, key=lambda x: len(x[0]))[1] if found else None


def _tilde(folder, home):
    """/home/kkj/dev/x -> ~/dev/x, the way project folders are written."""
    home = (home or "").rstrip("/")
    return "~" + folder[len(home):] if home and (folder == home or folder.startswith(home + "/")) else folder


_folders = {}  # "device/agent" -> (expires, folder): /me looks up where an agent works


async def agent_folder(device_name, agent, fresh=False):
    hit = _folders.get(f"{device_name}/{agent}")
    if hit and hit[0] > time.time() and not fresh:
        return hit[1]
    result = await agents_call(find_device(device_name), "list")
    for a in result.get("agents", []):
        _folders[f"{device_name}/{a['name']}"] = (time.time() + 300, a["folder"])
    return _folders.get(f"{device_name}/{agent}", (0, None))[1]


def brain_app():
    """The read-only brain agents reach through brain.sock: plain text, made for reading."""
    def text(fn):
        async def handler(request):
            try:
                out = fn(request)
                if asyncio.iscoroutine(out):
                    out = await out
            except brain.Problem as e:
                return web.Response(status=404, text=str(e) + "\n")
            except web.HTTPNotFound:
                return web.Response(status=404, text="No such device or agent.\n")
            return web.Response(text=out, content_type="text/markdown")
        return handler

    async def me(r):
        device_name, agent = r.match_info["device"], r.match_info["agent"]
        return brain.read_me(f"{device_name}/{agent}", brain.home_of(f"{device_name}/{agent}"))

    async def here(r):
        """The project (and rules) for a session Fleet didn't start: by machine and folder."""
        device_name = await device_for_host(r.query.get("host"))
        folder = r.query.get("folder", "")
        home = await project_for_folder(device_name, _tilde(folder, r.query.get("home")))
        if home:
            return brain.read_rules() + "\n" + brain.read_project(home)
        return (brain.read_rules() + f"\nNo project's agents work in {folder} on {device_name}. Projects:\n"
                + brain.read_projects())

    async def save(r):
        """POST: file a session into the brain now, from /fleet-save. Form fields: agent (FLEET_AGENT),
        or host, file (the session's .jsonl), folder and home ($HOME, to match project folders)."""
        form = await r.post()
        extra = {"limit": 120000}  # a save reads bigger slices: fewer writer calls for a long session
        if form.get("project"):  # file it under this project, whatever its folder
            extra["project"] = brain.project(form["project"])["name"]
        if form.get("agent"):
            device_name, _, agent = form["agent"].partition("/")
            find_device(device_name)
            result = await digest({"device": device_name, "agent": agent, "force": True} | extra)
        else:
            if not form.get("file"):
                raise brain.Problem("Say which session to save: file=PATH of its .jsonl, or agent=$FLEET_AGENT.")
            device_name = await device_for_host(form.get("host"))
            result = await digest(extra | {"device": device_name, "path": form["file"], "force": True,
                                   "folder": _tilde(form.get("folder", ""), form.get("home"))})
        if "error" in result:
            raise brain.Problem(result["error"])
        if not result["written"]:
            return "Nothing new to save since the last save.\n"
        names = brain.titles(result["written"])
        lines = [f"Saved {result['notes']} notes from {result['chunks']} slice(s) of this session:"]
        lines += [f"- {i}: {names.get(i, i)}" for i in result["written"]]
        if not result["done"]:
            lines.append("There's more of this session left to read; run /fleet-save again.")
        return "\n".join(lines) + "\n"

    def depth(r):
        try:
            return max(1, min(3, int(r.query.get("depth", "1"))))
        except ValueError:
            return 1

    m = lambda r, k: r.match_info[k]
    app = web.Application()
    app.router.add_get("/", text(lambda r: "Fleet brain. Start with /me/$FLEET_AGENT, /rules, /projects or /search?q=WORDS.\n"))
    app.router.add_get("/me/{device}/{agent}", text(me))
    app.router.add_get("/here", text(here))
    app.router.add_post("/save", text(save))
    app.router.add_get("/me/", text(lambda r: brain.read_rules() + "\n$FLEET_AGENT is empty, so Fleet didn't start you. Projects:\n"
                                    + brain.read_projects()))
    app.router.add_get("/rules", text(lambda r: brain.read_rules()))
    app.router.add_get("/projects", text(lambda r: brain.read_projects()))
    app.router.add_get("/search", text(lambda r: brain.read_search(r.query.get("q", ""), r.query.get("project") or None)))
    app.router.add_get("/path", text(lambda r: brain.read_path(r.query.get("from", ""), r.query.get("to", ""))))
    app.router.add_get("/n/{kind}/{slug}", text(lambda r: brain.read_note(f"{m(r, 'kind')}/{m(r, 'slug')}", depth(r))))
    app.router.add_get("/p/{name}", text(lambda r: brain.read_rules() + "\n" + brain.read_project(m(r, "name"))))
    app.router.add_get("/p/{name}/kind/{kind}", text(lambda r: brain.project(m(r, "name")) and brain.read_kind(m(r, "name"), m(r, "kind"))))
    app.router.add_get("/p/{name}/search", text(lambda r: brain.project(m(r, "name")) and brain.read_search(r.query.get("q", ""), m(r, "name"))))
    return app


async def forward_brain(device):
    """Keeps brain.sock reachable on a device: an ssh connection that forwards the device's
    ~/.local/state/fleet/brain.sock to this hub's, reconnecting whenever it drops."""
    delay = 5
    while True:
        where = await agents_call(device, "paths")
        if "error" not in where:
            remote = f"{where['state']}/brain.sock"
            base = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ControlPath=none"]
            # sshd won't replace a socket left from an earlier connection, so clear it first.
            rm = await asyncio.create_subprocess_exec(*base, device["ssh"], "mkdir", "-p", where["state"], ";",
                                                      "rm", "-f", remote, stdout=asyncio.subprocess.DEVNULL,
                                                      stderr=asyncio.subprocess.DEVNULL)
            await rm.wait()
            proc = await asyncio.create_subprocess_exec(
                *base, "-N", "-o", "ExitOnForwardFailure=yes", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=3",
                "-R", f"{remote}:{BRAIN_SOCKET}", device["ssh"], stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            started = time.time()
            try:
                await proc.wait()
            except asyncio.CancelledError:
                proc.kill()
                raise
            if time.time() - started > 60:
                delay = 5
        await asyncio.sleep(delay)
        delay = min(300, delay * 2)


async def keep_forwards():
    """One brain forward per device in config.toml, following additions and removals."""
    running = {}
    while True:
        wanted = {(d["name"], d["ssh"]): d for d in config.devices()}
        for key in list(running):
            if key not in wanted:
                running.pop(key).cancel()
        for key, d in wanted.items():
            if key not in running or running[key].done():
                running[key] = asyncio.create_task(forward_brain(d))
        await asyncio.sleep(60)


def ssh_options():
    """Shares one ssh connection per machine between device checks and terminals."""
    SSH_MUX.mkdir(mode=0o700, parents=True, exist_ok=True)
    return ["-o", "ControlMaster=auto", "-o", f"ControlPath={SSH_MUX}/%C", "-o", "ControlPersist=120"]


def spawn_shell(argv=None):
    """Forks a login shell (or argv, e.g. ssh to a device) on a new pty and returns (pid, master fd)."""
    user = pwd.getpwuid(os.getuid())
    pid, fd = pty.fork()
    if pid == 0:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("INVOCATION_ID", "JOURNAL_", "LISTEN_"))}
        env.update(TERM="xterm-256color", COLORTERM="truecolor", HOME=user.pw_dir, SHELL=user.pw_shell,
                   USER=user.pw_name, LOGNAME=user.pw_name)
        shell = user.pw_shell or "/bin/bash"
        try:
            os.chdir(user.pw_dir)
            if argv:
                os.execvpe(argv[0], argv, env)
            os.execve(shell, ["-" + os.path.basename(shell)], env)  # a leading "-" makes it a login shell
        finally:
            os._exit(127)
    os.set_blocking(fd, False)
    return pid, fd


def resize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def end_shell(pid):
    """SIGHUP like a closed ssh session, then SIGKILL if it hasn't exited within two seconds."""
    try:
        for sig in (signal.SIGHUP, signal.SIGKILL):
            os.kill(pid, sig)
            for _ in range(20):
                if os.waitpid(pid, os.WNOHANG) != (0, 0):
                    return
                await asyncio.sleep(0.1)
    except (ProcessLookupError, ChildProcessError):
        pass


DETACHED_SECONDS = 30 * 60  # how long a shell outlives its last connection
REPLAY_BYTES = 256 * 1024  # recent output kept to repaint the screen on reconnect
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")
TMUX_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
EXITED, TAKEN_OVER = 4000, 4001  # websocket close codes the page acts on
shells = {}  # session id -> Shell


class Shell:
    """A shell on a pty that outlives its websocket: a dropped connection leaves it running for
    DETACHED_SECONDS, and reconnecting with the same session id picks it up again."""

    def __init__(self, sid, target, argv):
        self.sid, self.target = sid, target
        self.pid, self.fd = spawn_shell(argv)
        self.recent = bytearray()
        self.ws = self.queue = self.expiry = None
        self.alive = True
        asyncio.get_running_loop().add_reader(self.fd, self._readable)

    def _readable(self):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # EIO: the shell exited
            data = b""
        if not data:
            self.close()
            return
        self.recent += data
        if len(self.recent) > REPLAY_BYTES:
            cut = self.recent.find(b"\n", len(self.recent) - REPLAY_BYTES)  # restart at a line boundary
            del self.recent[:cut + 1 if cut >= 0 else len(self.recent) - REPLAY_BYTES]
        if self.queue:
            self.queue.put_nowait(data)

    async def _write(self, data):
        while data:
            try:
                data = data[os.write(self.fd, data):]
            except BlockingIOError:
                await asyncio.sleep(0.01)

    async def attach(self, ws, reattached):
        if self.ws is not None:  # the same session opened in another tab: that one lets go
            await self.ws.close(code=TAKEN_OVER, message=b"opened elsewhere")
        if self.expiry:
            self.expiry.cancel()
            self.expiry = None
        self.ws, queue = ws, asyncio.Queue()
        self.queue = queue
        queue.put_nowait(bytes(self.recent))  # the page clears its screen and repaints from this

        async def send():
            while (data := await queue.get()) is not None:
                await ws.send_bytes(data)
            await ws.close(code=EXITED, message=b"shell exited")

        sender = asyncio.create_task(send())
        first_resize = reattached
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await self._write(msg.data)
                elif msg.type == WSMsgType.TEXT:
                    cols, rows = (max(1, int(n)) for n in json.loads(msg.data)["resize"])
                    if first_resize:  # wiggle the size so full-screen programs redraw themselves
                        resize(self.fd, cols, max(1, rows - 1))
                        first_resize = False
                    resize(self.fd, cols, rows)
        except (OSError, ValueError):  # the shell went away mid-message, or a bad resize
            pass
        finally:
            sender.cancel()
            if self.ws is ws:
                self.ws = self.queue = None
                if self.alive:
                    self.expiry = asyncio.get_running_loop().call_later(DETACHED_SECONDS, self.close)

    def close(self):
        if not self.alive:
            return
        self.alive = False
        shells.pop(self.sid, None)
        if self.expiry:
            self.expiry.cancel()
        loop = asyncio.get_running_loop()
        try:
            loop.remove_reader(self.fd)
        except (ValueError, OSError):
            pass
        os.close(self.fd)
        if self.queue:
            self.queue.put_nowait(None)
        loop.create_task(end_shell(self.pid))


async def terminal(request):
    """Binary frames carry terminal bytes both ways; text frames carry {"resize": [cols, rows]}.
    ?device=NAME opens the shell on that machine from config.toml instead, over ssh.
    ?agent=NAME joins that agent's tmux session instead of a shell; ?tmux=NAME, a session on the
    default tmux server. ?session=ID resumes that shell if it's still running, or starts one under that id."""
    device = find_device(request.query.get("device"))
    agent, tmux = request.query.get("agent"), request.query.get("tmux")
    if agent is not None and not agents.NAME.match(agent):
        raise web.HTTPBadRequest(text="bad agent name")
    if tmux is not None and not TMUX_NAME.match(tmux):
        raise web.HTTPBadRequest(text="bad tmux session name")
    command = agents.attach_command(agent, tmux) if agent or tmux else None
    if device is None:
        argv = ["sh", "-c", command] if command else None
    else:
        argv = ["ssh", "-tt", "-o", "ConnectTimeout=10", *ssh_options(), device["ssh"], *([command] if command else [])]
    # What this session shows; a session id can't be reused to open something else.
    target = f"{device['name'] if device else HOST}/{'agent:' + agent if agent else 'tmux:' + tmux if tmux else 'shell'}"
    sid = request.query.get("session", "")
    if not SESSION_ID.match(sid):
        raise web.HTTPBadRequest(text="session must be 8-64 letters, digits or dashes")
    shell = shells.get(sid)
    if shell and shell.target != target:
        raise web.HTTPConflict(text="that session belongs to something else")
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)
    reattached = shell is not None
    if shell is None and agent:
        await agents_call(device, "wake", {"name": agent}, timeout=40)  # opening an agent that's asleep wakes it
    if shell is None:
        shell = shells[sid] = Shell(sid, target, argv)
    await shell.attach(ws, reattached)
    return ws


def main():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o750)
    SOCKET.unlink(missing_ok=True)
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/terminal", terminal)
    app.router.add_get("/devices", devices)
    app.router.add_get("/update", update)
    app.router.add_get("/agents", agents_list)
    app.router.add_get("/agents/options", agents_options)
    app.router.add_post("/agents/{action}", agents_action)
    app.router.add_get("/projects", projects_list)
    app.router.add_get("/projects/{name}", project_detail)
    app.router.add_post("/projects/{action:create|delete|instructions}", projects_action)
    app.router.add_get("/brain", brain_overview)
    app.router.add_get("/brain/note", brain_note)
    app.router.add_get("/brain/search", brain_search)
    app.router.add_post("/brain/{action:digest|home|edit|delete|rule}", brain_action)
    app.router.add_route("*", "/brain/writer", brain_writer)

    async def serve():
        runner = web.AppRunner(app)
        await runner.setup()
        await web.UnixSite(runner, str(SOCKET)).start()
        # Group-writable so fleet-web, which joins this user's group, can connect; nobody else can.
        os.chmod(SOCKET, 0o660)
        brain_runner = forwards = None
        if HUB_FILE.exists():
            # Agents here read brains on this socket, and on other devices through its ssh forwards.
            # Only this user can connect.
            BRAIN_SOCKET.unlink(missing_ok=True)
            brain_runner = web.AppRunner(brain_app())
            await brain_runner.setup()
            await web.UnixSite(brain_runner, str(BRAIN_SOCKET)).start()
            os.chmod(BRAIN_SOCKET, 0o600)
            forwards = asyncio.create_task(keep_forwards())
        stop = asyncio.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig, stop.set)
        await stop.wait()
        if brain_runner:
            forwards.cancel()
            await brain_runner.cleanup()
        await runner.cleanup()

    asyncio.run(serve())


if __name__ == "__main__":
    main()
