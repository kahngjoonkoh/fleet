"""fleet-web: the Fleet dashboard. This step serves sign-in and admin-managed accounts only;
there is no sign-up page, and only an admin can create accounts."""

import asyncio
import os
import re
import sqlite3
import time
from collections import defaultdict, deque
from pathlib import Path

import aiohttp
import jinja2
import markupsafe
from aiohttp import web

from . import store

HERE = Path(__file__).parent
COOKIE = "__Host-fleet_session"  # __Host- makes browsers require Secure, Path=/ and no Domain
ORIGINS = {os.environ["FLEET_PUBLIC_ORIGIN"]} | {
    o for o in os.environ.get("FLEET_EXTRA_ORIGINS", "").split(",") if o}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
OPEN_PATHS = {"/login", "/healthz", "/manifest.webmanifest", "/sw.js"}
# fleetd on this host, mounted in by compose; it runs the Terminal tab's shells.
FLEETD_SOCK = os.environ.get("FLEETD_SOCK", "/run/fleet/fleetd.sock")
# Devices tab: CPU, memory, disk and GPUs only when installed with --show-hardware.
SHOW_HARDWARE = os.environ.get("FLEET_SHOW_HARDWARE") == "1"
INSTALL_URL = os.environ.get("FLEET_INSTALL_URL", "https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/install.sh")
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; "
       "manifest-src 'self'; worker-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'")
# xterm.js styles its rows by inserting <style> elements, which need 'unsafe-inline' for styles only.
TERM_CSP = CSP.replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")

templates = jinja2.Environment(loader=jinja2.FileSystemLoader(HERE / "templates"), autoescape=True)
# Cloudflare's Email Obfuscation rewrites plain-text addresses into links that need its script, which the
# CSP blocks. Splitting the address around a tag keeps its scanner from matching while it reads the same.
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
    html = templates.get_template(name).render(user=user, csrf=user["csrf"] if user else "", **ctx)
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
    response = await handler(request)
    if response.prepared:  # a websocket that has already closed
        return response
    response.headers.update({
        "Content-Security-Policy": TERM_CSP if path == "/terminal" else CSP,
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "same-origin",
        "Cache-Control": "no-store",
    })
    return response


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


async def home(request):
    return render(request, "home.html", tab="instances")


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


async def devices_page(request):
    error = None
    try:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(FLEETD_SOCK),
                                         timeout=aiohttp.ClientTimeout(total=25)) as session:
            async with session.get("http://fleetd/devices", params={"hardware": int(SHOW_HARDWARE)}) as r:
                devices = (await r.json())["devices"]
    except (aiohttp.ClientError, OSError, TimeoutError, KeyError):
        devices, error = [], "fleetd isn't answering on this host."
    # Only machines running Fleet; ones merely reachable over ssh stay off the list.
    devices = [d for d in devices if not d["online"] or d["info"]["fleetd"] != "not installed"]
    return render(request, "devices.html", tab="devices", error=error, devices=devices, hardware=SHOW_HARDWARE,
                  install_cmd=f"curl -fsSL {INSTALL_URL} | sh")


async def terminal_page(request):
    require_admin(request)
    return render(request, "terminal.html", tab="terminal")


async def terminal_socket(request):
    """Relays the browser's terminal websocket to fleetd, which runs the shell."""
    require_admin(request)
    session = aiohttp.ClientSession(connector=aiohttp.UnixConnector(FLEETD_SOCK))
    try:
        upstream = await session.ws_connect("http://fleetd/terminal", max_msg_size=0)
    except (aiohttp.ClientError, OSError):
        await session.close()
        return web.Response(status=502, text="fleetd isn't running on this host.")
    store.audit(request.app["db"], request["user"]["email"], "terminal-open", ip=client_ip(request))
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
        await ws.close()
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
    app.router.add_get("/login", login_page)
    app.router.add_post("/login", login)
    app.router.add_post("/logout", logout)
    app.router.add_get("/", home)
    app.router.add_get("/settings", moved("/settings/account"))
    app.router.add_get("/account/password", moved("/settings/account"))
    app.router.add_get("/admin/users", moved("/settings/users"))
    app.router.add_get("/settings/account", account_page)
    app.router.add_post("/settings/account", change_password)
    app.router.add_get("/settings/users", list_users)
    app.router.add_post("/settings/users", create_user)
    app.router.add_post(r"/settings/users/{id:\d+}/{action}", user_action)
    app.router.add_get("/devices", devices_page)
    app.router.add_get("/terminal", terminal_page)
    app.router.add_get("/terminal/ws", terminal_socket)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/healthz", healthz)
    app.router.add_static("/static", HERE / "static")
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=8080, access_log=None)
