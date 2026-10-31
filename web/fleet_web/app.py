"""fleet-web: the Fleet dashboard. This step serves sign-in and admin-managed accounts only;
there is no sign-up page, and only an admin can create accounts."""

import asyncio
import hashlib
import json
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
import jinja2
import markupsafe
from aiohttp import web

from . import md, store

HERE = Path(__file__).parent
COOKIE = "__Host-fleet_session"  # __Host- makes browsers require Secure, Path=/ and no Domain
ORIGINS = {os.environ["FLEET_PUBLIC_ORIGIN"]} | {
    o for o in os.environ.get("FLEET_EXTRA_ORIGINS", "").split(",") if o}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OPEN_PATHS = {"/login", "/healthz", "/manifest.webmanifest", "/sw.js", "/speculation-rules.json"}
# fleetd on this host, mounted in by compose; it runs the Terminal tab's shells.
FLEETD_SOCK = os.environ.get("FLEETD_SOCK", "/run/fleet/fleetd.sock")
# Devices tab: CPU, memory, disk and GPUs only when installed with --show-hardware.
SHOW_HARDWARE = os.environ.get("FLEET_SHOW_HARDWARE") == "1"
VERSION = os.environ.get("FLEET_VERSION", "dev")
INSTALL_URL = os.environ.get("FLEET_INSTALL_URL", "https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/install.sh")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "manifest-src 'self'; worker-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
# xterm.js styles its rows by inserting <style> elements, which need 'unsafe-inline' for styles only.
TERM_CSP = CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")

templates = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
# Cloudflare's Email Obfuscation rewrites plain-text addresses into links that need its script, which the
# CSP blocks. Splitting the address around a tag keeps its scanner from matching while it reads the same.
# Static files are linked as /static/NAME?v=HASH, the hash of every static file, so browsers can keep
# them for a year and still fetch new ones after an update.
STATIC_V = hashlib.sha256(b"".join(f.read_bytes() for f in sorted((HERE / "static").rglob("*")) if f.is_file())).hexdigest()[:10]
templates.globals["static"] = lambda name: f"/static/{name}?v={STATIC_V}"
templates.filters["email"] = lambda e: markupsafe.Markup("<span>@</span>").join(e.split("@", 1))
def _size(n):
    # 1024-based, as free(1) and nvidia-smi count, so a 3090 reads 24 GB rather than 25.8.
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") or n >= 100 else f"{n:.1f} {unit}"
        n /= 1024


def _duration(s):
    d, h, m = s // 86400, s % 86400 // 3600, s % 3600 // 60
    return f"{d}d {h}h" if d else f"{h}h {m}m" if h else f"{m}m"


templates.filters["size"] = _size
templates.filters["duration"] = _duration
templates.filters["minutes"] = lambda m: f"{m} min" if m < 60 else f"{m // 60} h" if m % 60 == 0 else f"{m // 60} h {m % 60} min"
templates.tests["match_tmux"] = lambda name: bool(TMUX_NAME.match(name))
templates.filters["when"] = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "never"


def client_ip(request):
    # Only cloudflared reaches this container, and it sets CF-Connecting-IP to the visitor's address.
    return request.headers.get("CF-Connecting-IP") or request.remote


class Throttle:
    """Failed sign-ins: at most 10 per IP and 5 per email in any 15 minutes."""

    WINDOW = 15 * 60

    def __init__(self):
        self.fails = defaultdict(deque)

    def blocked(self, *keys):
        now = time.time()
        for key, limit in keys:
            q = self.fails[key]
            while q and q[0] < now - self.WINDOW:
                q.popleft()
            if len(q) >= limit:
                return True
        return False

    def fail(self, *keys):
        for key, _ in keys:
            self.fails[key].append(time.time())


def render(request, name, status=200, **ctx):
    user = request.get("user")
    html = templates.get_template(name).render(user=user, csrf=user["csrf"] if user else "",
                                               updates=request.app.get("update_count", 0), **ctx)
    return web.Response(text=html, content_type="text/html", status=status)


def is_websocket(request):
    return request.headers.get("Upgrade", "").lower() == "websocket"


def redirect(path):
    raise web.HTTPSeeOther(path)


@web.middleware
async def security(request, handler):
    db = request.app["db"]
    request["user"] = store.session_user(db, request.cookies.get(COOKIE))
    if request.method == "POST" or is_websocket(request):
        origin = request.headers.get("Origin") or request.headers.get("Referer", "")
        if not any(origin == o or origin.startswith(o + "/") for o in ORIGINS):
            raise web.HTTPForbidden(text="Cross-site request refused.")
    path = request.path
    if not path.startswith("/static/") and path not in OPEN_PATHS:
        user = request["user"]
        if user is None:
            redirect("/login")
        if request.method == "POST":
            form = await request.post()
            if form.get("csrf") != user["csrf"]:
                raise web.HTTPForbidden(text="Form expired. Reload the page and try again.")
        if user["must_change"] and path not in ("/settings/account", "/logout"):
            redirect("/settings/account")
    try:
        response = await handler(request)
    except web.HTTPException as e:  # redirects and errors are raised; they get the same headers
        response = e
    if response.prepared:  # a websocket that has already closed
        return response
    response.headers.update({
        "Content-Security-Policy": TERM_CSP if path == "/terminal" else CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "Cache-Control": cache_control(request),
    })
    if response.content_type == "text/html":
        # Chrome fetches a page as soon as you hover over or touch a link to it (see /speculation-rules.json).
        response.headers["Speculation-Rules"] = '"/speculation-rules.json"'
    if isinstance(response, web.HTTPException):
        raise response
    return response


def cache_control(request):
    """Versioned static files: a year. Pages that show one-time passwords, and signing in: never kept.
    Other pages: kept but rechecked every time, which still lets back and forward be instant."""
    if request.path.startswith("/static/"):
        return "public, max-age=31536000, immutable" if request.query.get("v") == STATIC_V else "no-cache"
    if request.path in ("/settings/users", "/login") or request.path.startswith("/settings/users/"):
        return "no-store"
    return "private, no-cache"


def back(request, default="/"):
    """Where an action returns to: the page it was taken on (same site only), else `default`."""
    ref = urlparse(request.headers.get("Referer", ""))
    origin = f"{ref.scheme}://{ref.netloc}"
    if origin in ORIGINS and re.match(r"^/(projects(/[a-z0-9-]+)?)?$", ref.path or "/"):
        return ref.path or "/"
    return default


SPECULATION_RULES = json.dumps({"prefetch": [{"where": {"and": [
    {"href_matches": "/*"}, {"not": {"href_matches": "/terminal*"}}, {"not": {"href_matches": "/logout"}},
    {"not": {"href_matches": "/static/*"}}]}, "eagerness": "moderate"}]})


async def speculation_rules(request):
    return web.Response(text=SPECULATION_RULES, content_type="application/speculationrules+json")


async def login_page(request):
    if request["user"]:
        redirect("/")
    return render(request, "login.html")


async def login(request):
    form = await request.post()
    email = form.get("email", "").strip().lower()
    password = form.get("password", "")
    ip = client_ip(request)
    keys = (("ip:" + ip, 10), ("email:" + email, 5))
    throttle = request.app["throttle"]
    if throttle.blocked(*keys):
        return render(request, "login.html", status=429, email=email,
                      error="Too many failed attempts. Try again in 15 minutes.")
    db = request.app["db"]
    row = db.execute("SELECT id, pw_hash, disabled FROM users WHERE email = ?", (email,)).fetchone()
    ok = await asyncio.get_running_loop().run_in_executor(
        None, store.check_password, password, row["pw_hash"] if row else store.DUMMY_HASH)
    if not row or not ok or row["disabled"]:
        throttle.fail(*keys)
        store.audit(db, email, "login-failed", ip=ip)
        return render(request, "login.html", status=401, email=email, error="Wrong email or password.")
    token = store.start_session(db, row["id"], ip)
    store.audit(db, email, "login", ip=ip)
    response = web.HTTPSeeOther("/")
    response.set_cookie(COOKIE, token, max_age=store.SESSION_SECONDS, path="/", secure=True,
                        httponly=True, samesite="Lax")
    raise response


async def logout(request):
    store.end_session(request.app["db"], request.cookies.get(COOKIE))
    response = web.HTTPSeeOther("/login")
    response.del_cookie(COOKIE, path="/")
    raise response


# Settings > Agents: how long an agent may sit with no output and no keypress before it's put to sleep.
SLEEP_CHOICES = [0, 10, 30, 60, 120, 240, 480, 1440]  # minutes; 0 means never
SLEEP_DEFAULT = 30


def sleep_after(db):
    try:
        return int(store.get_setting(db, "sleep_after_minutes", SLEEP_DEFAULT))
    except ValueError:
        return SLEEP_DEFAULT


async def sleep_idle_agents(db, minutes):
    """Puts every agent that's been quiet for `minutes` to sleep, unless it's pinned or can't resume.
    Activity is the later of its last output and its last keypress, read on its own machine."""
    for d in (await fleetd_get("/agents"))["devices"]:
        for a in d.get("agents", []):
            quiet = d.get("now", time.time()) - ((a.get("pane") or {}).get("last_active") or time.time())
            if a["state"] == "running" and a["lazy"] and not a["pinned"] and quiet > minutes * 60:
                result = await fleetd_post("/agents/sleep", {"device": d["name"], "name": a["name"]})
                if "error" not in result:
                    store.audit(db, "fleet", "agent-sleep", f"{d['name']}/{a['name']} after {int(quiet // 60)}m quiet")


# --- project brains ---------------------------------------------------------------------
BRAIN_TYPES = ("claude", "pi")  # agents whose conversations Fleet can read
BUSY_DIGEST = 2 * 3600  # an agent that never goes quiet still gets a brain update this often
WRITER_MODES = ("subscription", "anthropic", "openai")


async def writer_config():
    """Who writes the brain; fleetd keeps it, so a /fleet-save from any machine uses the same."""
    return await fleetd_get("/brain/writer")


async def run_digest(app, device, agent, force=False):
    key = f"{device}/{agent}"
    if key in app["digesting"]:
        return {"error": "already updating"}
    app["digesting"].add(key)
    try:
        result = await fleetd_post("/brain/digest", {"device": device, "agent": agent, "force": force}, timeout=1800)
    except (aiohttp.ClientError, OSError, TimeoutError) as e:
        result = {"error": f"fleetd: {e or type(e).__name__}"}
    finally:
        app["digesting"].discard(key)
    if "error" in result:
        app["brain_error"] = (int(time.time()), f"{key}: {result['error']}")
    elif result.get("chunks"):
        app["brain_error"] = None
        store.audit(app["db"], "fleet", "brain-update", f"from {key}: {result['notes']} notes")
    return result


async def digest_quiet_agents(app, agents, quiet_for):
    """Starts a brain update for each Claude Code or Pi agent that has just gone quiet (the moment it
    would sleep, pinned or not), or that has been busy for BUSY_DIGEST without one. Each runs in the
    background; fleetd skips agents with nothing new to read. Agents outside any project count too:
    the writer files their notes under whatever projects they concern."""
    seen, last = app["digested"], app["last_digest"]
    for a in agents:
        key, active = f"{a['device']}/{a['name']}", (a.get("pane") or {}).get("last_active")
        if a["type"] not in BRAIN_TYPES or a["state"] != "running" or not active:
            continue
        now = a.get("now") or time.time()
        last.setdefault(key, now)
        quiet = now - active > quiet_for
        if (quiet and seen.get(key) != active) or (not quiet and now - last[key] > BUSY_DIGEST):
            seen[key], last[key] = active, now
            asyncio.create_task(run_digest(app, a["device"], a["name"]))


async def watch_idle(app):
    """Every minute: brain updates for agents that went quiet, then sleep for idle agents."""
    while True:
        await asyncio.sleep(60)
        minutes = sleep_after(app["db"])
        try:
            devices = (await fleetd_get("/agents"))["devices"]
            agents = [a | {"device": d["name"], "now": d.get("now")} for d in devices for a in d.get("agents", [])]
            await digest_quiet_agents(app, agents, (minutes or SLEEP_DEFAULT) * 60)
            if minutes:
                await sleep_idle_agents(app["db"], minutes)
        except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
            pass


async def home(request, status=200, error=None):
    try:
        devices = (await fleetd_get("/agents"))["devices"]
    except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
        devices, error = [], error or "fleetd isn't answering on this host."
    agents = [a | {"device": d["name"]} for d in devices for a in d.get("agents", [])]
    others = [s | {"device": d["name"]} for d in devices for s in d.get("tmux", [])]
    unreachable = [d for d in devices if "error" in d]
    return render(request, "home.html", status=status, tab="agents", agents=agents, others=others,
                  unreachable=unreachable, error=error, now=int(time.time()), sleep_after=sleep_after(request.app["db"]))


AGENT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
TMUX_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


async def new_agent_page(request, status=200, error=None, form=None):
    require_admin(request)
    try:
        devices = [d for d in (await fleetd_get("/agents/options"))["devices"] if "error" not in d]
    except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
        devices, error = [], "fleetd isn't answering on this host."
    # Every type any device offers; the page's script narrows the list to the chosen device's.
    types = {t["id"]: t["label"] for d in devices for t in d["types"]}
    try:
        projects = (await fleetd_get("/projects"))["projects"]
    except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
        projects = []
    return render(request, "agent_new.html", status=status, tab="agents", devices=devices, types=types, error=error,
                  projects=projects,
                  form=form or {"folder": request.query.get("folder", "~"), "device": request.query.get("device", ""),
                                "type": "claude" if "claude" in types else "shell", "project": request.query.get("project", "")},
                  sleep_after=sleep_after(request.app["db"]))


async def create_agent(request):
    require_admin(request)
    form = await request.post()
    values = {k: form.get(k, "").strip() for k in ("name", "device", "type", "folder", "command")}
    values["remote_control"] = "on" if form.get("remote_control") == "on" else "off"
    if not AGENT_NAME.match(values["name"]):
        return await new_agent_page(request, 400, "Names are 1-32 letters, digits, dashes or underscores, "
                                    "starting with a letter or digit.", values)
    if not DEVICE_NAME.match(values["device"]):
        return await new_agent_page(request, 400, "Pick a device.", values)
    try:
        result = await fleetd_post("/agents/create", {**values, "remote_control": values["remote_control"] == "on"})
    except (aiohttp.ClientError, OSError, TimeoutError):
        result = {"error": "fleetd isn't answering on this host."}
    if "error" in result:
        return await new_agent_page(request, 400, f"{values['device']}: {result['error']}", values)
    if PROJECT_NAME.match(form.get("project", "")):
        await fleetd_post("/brain/home", {"device": values["device"], "agent": values["name"], "project": form["project"]})
    store.audit(request.app["db"], request["user"]["email"], "agent-create",
                f"{values['device']}/{values['name']} {values['type']}", client_ip(request))
    redirect("/")


async def agent_edit_page(request, status=200, error=None):
    require_admin(request)
    device, name = request.match_info["device"], request.match_info["name"]
    try:
        agent = next((a for a in await all_agents() if a["device"] == device and a["name"] == name), None)
        projects = (await fleetd_get("/projects"))["projects"]
    except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
        raise fleetd_down()
    if agent is None:
        raise web.HTTPNotFound(text="There's no such agent.")
    return render(request, "agent_edit.html", status=status, tab="agents", a=agent, projects=projects, error=error)


def agent_rc_changed(form):
    return "was_rc" in form and (form.get("remote_control") == "on") != (form.get("was_rc") == "1")


async def save_agent(request):
    require_admin(request)
    device, name = request.match_info["device"], request.match_info["name"]
    if not DEVICE_NAME.match(device) or not AGENT_NAME.match(name):
        raise web.HTTPNotFound()
    form = await request.post()
    choice = form.get("project", "")
    if choice and not PROJECT_NAME.match(choice):
        raise web.HTTPBadRequest(text="Pick a project.")
    new_name = form.get("name", name).strip()
    if new_name != name:
        if not AGENT_NAME.match(new_name):
            return await agent_edit_page(request, 400, "Names are 1-32 letters, digits, dashes or underscores, starting with a letter or digit.")
        result = await fleetd_post("/agents/rename", {"device": device, "name": name, "new": new_name})
        if "error" in result:
            return await agent_edit_page(request, 400, result["error"])
        store.audit(request.app["db"], request["user"]["email"], "agent-rename", f"{device}/{name} -> {new_name}", client_ip(request))
        name = new_name
    result = await fleetd_post("/brain/home", {"device": device, "agent": name, "project": choice})
    if "error" not in result and agent_rc_changed(form):
        result = await fleetd_post("/agents/settings", {"device": device, "name": name, "remote_control": form.get("remote_control") == "on"})
    if "error" in result:
        return await agent_edit_page(request, 400, result["error"])
    store.audit(request.app["db"], request["user"]["email"], "agent-edit", f"{device}/{name} project={choice or 'none'}",
                client_ip(request))
    redirect("/")


async def agent_action(request):
    require_admin(request)
    device, name, action = (request.match_info[k] for k in ("device", "name", "action"))
    if not DEVICE_NAME.match(device) or not AGENT_NAME.match(name):
        raise web.HTTPNotFound()
    body = {"device": device, "name": name}
    if action in ("pin", "unpin"):
        action, body["pinned"] = "pin", action == "pin"
    try:
        result = await fleetd_post(f"/agents/{action}", body)
    except (aiohttp.ClientError, OSError, TimeoutError):
        result = {"error": "fleetd isn't answering on this host."}
    if "error" in result:
        return await home(request, 400, f"{device}/{name}: {result['error']}")
    verb = ("pin" if body["pinned"] else "unpin") if "pinned" in body else action
    store.audit(request.app["db"], request["user"]["email"], "agent-" + verb, f"{device}/{name}", client_ip(request))
    redirect(back(request))


PROJECT_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
NOTE_ID = re.compile(r"^[a-z][a-z0-9-]{0,31}/[a-z0-9][a-z0-9-]{0,95}$")


def note_url(note_id):
    return f"/notes/{note_id}"


async def all_agents():
    devices = (await fleetd_get("/agents"))["devices"]
    return [a | {"device": d["name"]} for d in devices for a in d.get("agents", [])]


def fleetd_down():
    return web.HTTPBadGateway(text="fleetd isn't answering on this host.")


async def projects_page(request, status=200, error=None, notice=None):
    try:
        projects = (await fleetd_get("/projects"))["projects"]
        agents, overview = await all_agents(), await fleetd_get("/brain")
    except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
        projects, agents, overview, error = [], [], {"graph": {"nodes": []}, "rules": {}}, "fleetd isn't answering on this host."
    for p in projects:
        mine = [a for a in agents if a.get("project") == p["name"]]
        p["agents"], p["running"] = len(mine), sum(a["state"] == "running" for a in mine)
    for n in overview["graph"]["nodes"]:
        n["url"] = f"/projects/{n['title']}" if n["kind"] == "project" else note_url(n["id"])
    loose = [a for a in agents if not a.get("project")]
    return render(request, "projects.html", status=status, tab="projects", projects=projects, graph=overview["graph"],
                  rules=overview["rules"], loose=loose, error=error, notice=notice)


async def project_page(request, status=200, error=None, notice=None):
    name = request.match_info["name"]
    if not PROJECT_NAME.match(name):
        raise web.HTTPNotFound()
    try:
        data = await fleetd_get(f"/projects/{name}")
        agents = [a for a in await all_agents() if a.get("project") == name]
    except (KeyError, aiohttp.ContentTypeError):  # fleetd's 404 is text, not JSON
        raise web.HTTPNotFound(text=f"There's no project called {name}.")
    except (aiohttp.ClientError, OSError, TimeoutError):
        raise fleetd_down()
    by_kind = {}
    for note_id, n in data["notes"].items():
        if n["kind"] != "overview" and n["scope"] != "global":
            by_kind.setdefault(n["kind"], []).append((note_id, n))
    for items in by_kind.values():
        items.sort(key=lambda i: (i[1].get("status") in ("done", "fixed", "superseded"), i[1]["title"].lower()))
    sessions = sorted(by_kind.pop("session", []), key=lambda i: i[0], reverse=True)
    for node in data["graph"]["nodes"]:
        node["url"] = note_url(node["id"])
    brain_error = request.app.get("brain_error")
    return render(request, "project.html", status=status, tab="projects", p=data["project"],
                  overview=md.render(data["overview"], note_url), by_kind=by_kind, sessions=sessions[:12],
                  graph=data["graph"], agents=agents, now=int(time.time()), sleep_after=sleep_after(request.app["db"]),
                  error=error, notice=notice, brain_error=brain_error[1] if brain_error else None,
                  writer=(await writer_config())["mode"])


async def note_page(request, status=200, error=None, notice=None):
    note_id = f"{request.match_info['kind']}/{request.match_info['slug']}"
    if not NOTE_ID.match(note_id):
        raise web.HTTPNotFound()
    try:
        data = await fleetd_get("/brain/note", id=note_id)
    except (KeyError, aiohttp.ContentTypeError):
        raise web.HTTPNotFound(text="There's no such note.")
    except (aiohttp.ClientError, OSError, TimeoutError):
        raise fleetd_down()
    titles, n = data["titles"], data["note"]
    links = [(rel.replace("_", " "), t, titles.get(t, t)) for rel, ts in n["links"].items() for t in ts]
    back = [(rel.replace("_", " "), src, titles.get(src, src)) for src, rel in data["backlinks"]]
    return render(request, "note.html", status=status, tab="projects", note_id=note_id, n=n, all_projects=data["projects"],
                  body=md.render(n["body"], note_url), links=links, back=back, error=error, notice=notice,
                  editing=request.query.get("edit") == "1" and request["user"]["is_admin"])


async def note_action(request):
    require_admin(request)
    note_id = f"{request.match_info['kind']}/{request.match_info['slug']}"
    if not NOTE_ID.match(note_id):
        raise web.HTTPNotFound()
    form, action = await request.post(), request.match_info["action"]
    body = {"id": note_id}
    if action == "edit":
        body |= {"title": form.get("title", ""), "body": form.get("body", ""), "status": form.get("status", ""),
                 "scope": form.get("scope", "project"), "projects": form.getall("projects", [])}
    try:
        result = await fleetd_post(f"/brain/{action}", body)
    except (aiohttp.ClientError, OSError, TimeoutError):
        raise fleetd_down()
    if "error" in result:
        return await note_page(request, 400, error=result["error"])
    store.audit(request.app["db"], request["user"]["email"], "note-" + action, note_id, client_ip(request))
    redirect("/projects" if action == "delete" else note_url(note_id))


async def add_rule(request):
    require_admin(request)
    form = await request.post()
    title, body = form.get("title", "").strip(), form.get("body", "").strip()
    if not title:
        return await projects_page(request, 400, error="Write the rule.")
    result = await fleetd_post("/brain/rule", {"title": title, "body": body})
    if "error" in result:
        return await projects_page(request, 400, error=result["error"])
    store.audit(request.app["db"], request["user"]["email"], "rule-add", result["id"], client_ip(request))
    redirect("/projects")


async def search_page(request):
    q, project = request.query.get("q", "").strip(), request.query.get("project", "")
    results, error = [], None
    if q:
        try:
            data = await fleetd_get("/brain/search", q=q, **({"project": project} if PROJECT_NAME.match(project) else {}))
        except (aiohttp.ClientError, OSError, TimeoutError):
            raise fleetd_down()
        results, error = data.get("results", []), data.get("error")
    return render(request, "search.html", tab="projects", q=q, project=project, results=results, error=error)


async def new_project_page(request, status=200, error=None, form=None):
    require_admin(request)
    return render(request, "project_new.html", status=status, tab="projects", error=error, form=form or {})


async def create_project(request):
    require_admin(request)
    form = await request.post()
    name = form.get("name", "").strip()
    if not PROJECT_NAME.match(name):
        return await new_project_page(request, 400, "Project names are 1-48 lowercase letters, digits or dashes.", {"name": name})
    try:
        result = await fleetd_post("/projects/create", {"name": name})
    except (aiohttp.ClientError, OSError, TimeoutError):
        result = {"error": "fleetd isn't answering on this host."}
    if "error" in result:
        return await new_project_page(request, 400, result["error"], {"name": name})
    store.audit(request.app["db"], request["user"]["email"], "project-create", name, client_ip(request))
    redirect(f"/projects/{name}")


async def project_action(request):
    require_admin(request)
    name, action = request.match_info["name"], request.match_info["action"]
    if not PROJECT_NAME.match(name):
        raise web.HTTPNotFound()
    db, notice = request.app["db"], None
    try:
        if action == "delete":
            result = await fleetd_post("/projects/delete", {"name": name})
        elif action == "instructions":
            result = await fleetd_post("/projects/instructions", {"name": name})
            if "error" not in result:
                notice = ("Updated " + "; ".join(result["changed"]) + ".") if result.get("changed") else \
                    "The agents' AGENTS.md and CLAUDE.md were already up to date."
        else:  # update the brain now from every agent in the project
            mine = [a for a in await all_agents() if a["type"] in BRAIN_TYPES and a.get("project") == name]
            results = [await run_digest(request.app, a["device"], a["name"], force=True) for a in mine]
            errors = [r["error"] for r in results if "error" in r]
            result = {"error": errors[0]} if errors else {"ok": True}
            notice = (f"Read {sum(r.get('chunks', 0) for r in results)} new slices from {len(mine)} agent"
                      f"{'' if len(mine) == 1 else 's'}; {sum(r.get('notes', 0) for r in results)} notes written or updated."
                      if mine else "This project has no Claude Code or Pi agents to read yet.")
    except (aiohttp.ClientError, OSError, TimeoutError):
        result = {"error": "fleetd isn't answering on this host."}
    if "error" in result:
        return await project_page(request, 400, error=result["error"])
    store.audit(db, request["user"]["email"], "project-" + action, name, client_ip(request))
    if action == "delete":
        redirect("/projects")
    return await project_page(request, notice=notice)


async def brain_settings_page(request, status=200, saved=False, error=None):
    require_admin(request)
    try:
        writer = await writer_config()
    except (aiohttp.ClientError, OSError, TimeoutError):
        raise fleetd_down()
    return render(request, "settings_brain.html", status=status, section="brain", writer=writer,
                  key_hint=writer["key_hint"], saved=saved, error=error, last_error=request.app.get("brain_error"))


async def save_brain_settings(request):
    require_admin(request)
    form = await request.post()
    mode = form.get("mode", "")
    if mode not in WRITER_MODES:
        raise web.HTTPBadRequest(text="Pick how the brain is written.")
    model, base_url, key = form.get("model", "").strip(), form.get("base_url", "").strip(), form.get("key", "").strip()
    has_key = bool((await writer_config())["key_hint"])
    if mode == "openai" and not re.match(r"^https?://", base_url):
        return await brain_settings_page(request, 400, error="An OpenAI-compatible API needs its base URL, e.g. https://api.example.com/v1")
    if mode != "subscription" and not key and not has_key:
        return await brain_settings_page(request, 400, error="Enter the API key.")
    if mode == "openai" and not model:
        return await brain_settings_page(request, 400, error="Enter the model name.")
    values = {"mode": mode, "model": model, "base_url": base_url}
    if key:  # a blank field keeps the saved key
        values["key"] = key
    if form.get("forget_key"):
        values["key"] = ""
    await fleetd_post("/brain/writer", values)
    store.audit(request.app["db"], request["user"]["email"], "settings-brain", f"{mode} {model}", client_ip(request))
    return await brain_settings_page(request, saved=True)


def moved(path):
    async def handler(request):
        redirect(path)
    return handler


async def account_page(request):
    return render(request, "account.html", section="account")


async def change_password(request):
    db, user = request.app["db"], request["user"]
    form = await request.post()
    current, new, repeat = form.get("current", ""), form.get("new", ""), form.get("repeat", "")
    stored = db.execute("SELECT pw_hash FROM users WHERE id = ?", (user["id"],)).fetchone()["pw_hash"]
    loop = asyncio.get_running_loop()
    error = None
    if not await loop.run_in_executor(None, store.check_password, current, stored):
        error = "Your current password is wrong."
    elif new != repeat:
        error = "The new passwords don't match."
    elif new == current:
        error = "Choose a password different from the current one."
    else:
        error = store.password_problem(new)
    if error:
        return render(request, "account.html", status=400, error=error, section="account")
    new_hash = await loop.run_in_executor(None, store.hash_password, new)
    db.execute("UPDATE users SET pw_hash = ?, must_change = 0 WHERE id = ?", (new_hash, user["id"]))
    # Sign out every other session of this account.
    db.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user["id"], user["token_hash"]))
    store.audit(db, user["email"], "password-changed", ip=client_ip(request))
    redirect("/")


async def agent_settings_page(request, status=200, saved=False):
    require_admin(request)
    return render(request, "settings_agents.html", status=status, section="agents", choices=SLEEP_CHOICES,
                  current=sleep_after(request.app["db"]), saved=saved)


async def save_agent_settings(request):
    require_admin(request)
    form = await request.post()
    try:
        minutes = int(form.get("sleep_after", ""))
    except ValueError:
        minutes = -1
    if minutes not in SLEEP_CHOICES:
        raise web.HTTPBadRequest(text="Pick one of the listed times.")
    store.set_setting(request.app["db"], "sleep_after_minutes", minutes)
    store.audit(request.app["db"], request["user"]["email"], "settings-sleep-after", f"{minutes}m", client_ip(request))
    return await agent_settings_page(request, saved=True)


def require_admin(request):
    if not request["user"]["is_admin"]:
        raise web.HTTPForbidden(text="Admins only.")


def users_page(request, status=200, **ctx):
    users = request.app["db"].execute("SELECT * FROM users ORDER BY is_admin DESC, email").fetchall()
    return render(request, "users.html", status=status, users=users, section="users", **ctx)


async def list_users(request):
    require_admin(request)
    return users_page(request)


async def create_user(request):
    require_admin(request)
    db, form = request.app["db"], await request.post()
    email = form.get("email", "").strip().lower()
    if not EMAIL.match(email):
        return users_page(request, 400, error="Enter a valid email address.", email=email)
    try:
        password = store.create_user(db, email, is_admin=form.get("admin") == "on")
    except sqlite3.IntegrityError:
        return users_page(request, 400, error=f"{email} already has an account.", email=email)
    store.audit(db, request["user"]["email"], "user-created", email, client_ip(request))
    return users_page(request, issued={"email": email, "password": password, "new": True})


async def user_action(request):
    require_admin(request)
    db, me = request.app["db"], request["user"]
    target = db.execute("SELECT * FROM users WHERE id = ?", (int(request.match_info["id"]),)).fetchone()
    if target is None:
        raise web.HTTPNotFound()
    action = request.match_info["action"]
    if target["id"] == me["id"] and action != "reset":
        return users_page(request, 400, error="You can't disable or delete your own account.")
    issued = None
    if action == "reset":
        issued = {"email": target["email"], "password": store.reset_password(db, target["id"]), "new": False}
    elif action == "disable":
        db.execute("UPDATE users SET disabled = 1 WHERE id = ?", (target["id"],))
        db.execute("DELETE FROM sessions WHERE user_id = ?", (target["id"],))
    elif action == "enable":
        db.execute("UPDATE users SET disabled = 0 WHERE id = ?", (target["id"],))
    elif action == "delete":
        if not target["disabled"]:
            return users_page(request, 400, error="Disable the account before deleting it.")
        db.execute("DELETE FROM users WHERE id = ?", (target["id"],))
    else:
        raise web.HTTPNotFound()
    store.audit(db, me["email"], "user-" + action, target["email"], client_ip(request))
    if issued and target["id"] == me["id"]:
        redirect("/login")  # resetting your own password signed you out
    return users_page(request, issued=issued)


async def fleetd_get(path, **params):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(FLEETD_SOCK),
                                     timeout=aiohttp.ClientTimeout(total=25)) as session:
        async with session.get("http://fleetd" + path, params=params) as r:
            return await r.json()


async def fleetd_post(path, body, timeout=60):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(FLEETD_SOCK),
                                     timeout=aiohttp.ClientTimeout(total=timeout)) as session:
        async with session.post("http://fleetd" + path, json=body) as r:
            return await r.json()


def update_command(source, hub):
    """What to run on a machine to update it: the curl line, or git pull in the checkout it came from."""
    if not source or source.endswith("/.local/share/fleet/src"):
        return f"curl -fsSL {INSTALL_URL} | sh" + (" -s -- --hub" if hub else "")
    return f"cd {source} && git pull && ./install.sh" + (" --hub" if hub else "")


async def check_devices(app):
    """Devices running Fleet, each with its update status; also refreshes the header's update dot."""
    devices = (await fleetd_get("/devices", hardware=int(SHOW_HARDWARE)))["devices"]
    # Only machines running Fleet; ones merely reachable over ssh stay off the list.
    devices = [d for d in devices if not d["online"] or d["info"]["fleetd"] != "not installed"]
    web_update = (await fleetd_get("/update", version=VERSION))["update"]
    for d in devices:
        if d["local"] and web_update and web_update["available"] and not (d["update"] or {}).get("available"):
            d["update"] = web_update
        d["update_cmd"] = update_command((d.get("info") or {}).get("fleet_source"), d["local"])
    app["update_count"] = sum(bool((d["update"] or {}).get("available")) for d in devices)
    return devices


async def watch_updates(app):
    while True:
        try:
            await check_devices(app)
        except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
            pass
        await asyncio.sleep(1800)


async def devices_page(request):
    error = None
    try:
        devices = await check_devices(request.app)
    except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
        devices, error = [], "fleetd isn't answering on this host."
    return render(request, "devices.html", tab="devices", error=error, devices=devices, hardware=SHOW_HARDWARE,
                  web_version=VERSION, install_cmd=f"curl -fsSL {INSTALL_URL} | sh")


DEVICE_NAME = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SESSION_ID = re.compile(r"^[A-Za-z0-9-]{8,64}$")


def terminal_target(request):
    """?device= (empty for the hub), plus ?agent= or ?tmux= to join a tmux session instead of a shell."""
    q = request.query
    device, agent, tmux = q.get("device", ""), q.get("agent", ""), q.get("tmux", "")
    if device and not DEVICE_NAME.match(device) or agent and not AGENT_NAME.match(agent) \
            or tmux and not TMUX_NAME.match(tmux):
        raise web.HTTPBadRequest(text="Bad device, agent or tmux session name.")
    return device, agent, tmux


async def terminal_page(request):
    require_admin(request)
    device, agent, tmux = terminal_target(request)
    try:
        devices = [d for d in await check_devices(request.app) if d["online"] and d["info"]["fleetd"] == "active"]
    except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
        devices = []
    hub = next((d["name"] for d in devices if d["local"]), "hub")
    return render(request, "terminal.html", tab="terminal", device=device, devices=devices, current=device or hub,
                  attached=agent or tmux, agent=agent, tmux=tmux)


async def terminal_socket(request):
    """Relays the browser's terminal websocket to fleetd, which runs the shell."""
    require_admin(request)
    device, agent, tmux = terminal_target(request)
    sid = request.query.get("session", "")
    if not SESSION_ID.match(sid):
        raise web.HTTPBadRequest(text="Bad session id.")
    params = {"session": sid} | {k: v for k, v in (("device", device), ("agent", agent), ("tmux", tmux)) if v}
    session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(FLEETD_SOCK))
    try:
        upstream = await session.ws_connect("http://fleetd/terminal", params=params, max_msg_size=0)
    except (aiohttp.ClientError, OSError):
        await session.close()
        return web.Response(status=502, text="fleetd isn't running on this host.")
    store.audit(request.app["db"], request["user"]["email"], "terminal-open",
                f"{device or 'hub'}{'/' + (agent or tmux) if agent or tmux else ''} {sid[:8]}",
                client_ip(request))
    # Pings keep Cloudflare from closing a quiet terminal after 100 seconds.
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=0)
    await ws.prepare(request)

    async def pump(src, dst):
        async for msg in src:
            if msg.type == aiohttp.WSMsgType.BINARY:
                await dst.send_bytes(msg.data)
            elif msg.type == aiohttp.WSMsgType.TEXT:
                await dst.send_str(msg.data)
            else:
                break

    tasks = [asyncio.create_task(pump(ws, upstream)), asyncio.create_task(pump(upstream, ws))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in tasks:
            t.cancel()
        await upstream.close()
        await session.close()
        # Pass on why fleetd ended it (shell exited, or opened in another tab); other drops reconnect.
        code = upstream.close_code if upstream.close_code in (4000, 4001) else aiohttp.WSCloseCode.GOING_AWAY
        await ws.close(code=code)
    return ws


async def manifest(request):
    return web.FileResponse(HERE / "static/manifest.webmanifest", headers={"Content-Type": "application/manifest+json"})


async def service_worker(request):
    # Served from the root so it may control every page; the browser rechecks it on each visit.
    return web.FileResponse(HERE / "static/sw.js", headers={"Content-Type": "text/javascript"})


async def healthz(request):
    return web.Response(text="ok")


def make_app():
    app = web.Application(middlewares=[security], client_max_size=64 * 1024)
    app["db"] = store.connect()
    app["throttle"] = Throttle()
    app["digesting"], app["digested"], app["last_digest"] = set(), {}, {}

    async def background(app):
        tasks = [asyncio.create_task(watch_updates(app)), asyncio.create_task(watch_idle(app))]
        yield
        for task in tasks:
            task.cancel()

    app.cleanup_ctx.append(background)
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login)
    app.router.add_post("/logout", logout)
    app.router.add_get("/", home)
    app.router.add_get("/agents/new", new_agent_page)
    app.router.add_post("/agents", create_agent)
    app.router.add_post(r"/agents/{device}/{name}/{action:start|stop|remove|pin|unpin}", agent_action)
    app.router.add_get("/settings", moved("/settings/account"))
    app.router.add_get("/account/password", moved("/settings/account"))
    app.router.add_get("/admin/users", moved("/settings/users"))
    app.router.add_get("/settings/account", account_page)
    app.router.add_post("/settings/account", change_password)
    app.router.add_get("/settings/agents", agent_settings_page)
    app.router.add_post("/settings/agents", save_agent_settings)
    app.router.add_get("/settings/users", list_users)
    app.router.add_post("/settings/users", create_user)
    app.router.add_post(r"/settings/users/{id:\d+}/{action}", user_action)
    app.router.add_get("/projects", projects_page)
    app.router.add_get("/projects/new", new_project_page)
    app.router.add_post("/projects", create_project)
    app.router.add_get("/projects/{name}", project_page)
    app.router.add_post("/projects/{name}/{action:digest|instructions|delete}", project_action)
    app.router.add_get(r"/notes/{kind}/{slug}", note_page)
    app.router.add_post(r"/notes/{kind}/{slug}/{action:edit|delete}", note_action)
    app.router.add_post("/rules", add_rule)
    app.router.add_get("/search", search_page)
    app.router.add_get("/agents/{device}/{name}/edit", agent_edit_page)
    app.router.add_post("/agents/{device}/{name}/edit", save_agent)
    app.router.add_get("/settings/brain", brain_settings_page)
    app.router.add_post("/settings/brain", save_brain_settings)
    app.router.add_get("/devices", devices_page)
    app.router.add_get("/terminal", terminal_page)
    app.router.add_get("/terminal/ws", terminal_socket)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/speculation-rules.json", speculation_rules)
    app.router.add_static("/static", HERE / "static")
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=8080, access_log=None)
