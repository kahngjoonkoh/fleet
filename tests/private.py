# Private projects on the web pages: the option on New project, the badge, switching, and that people who
# aren't admins don't see them. What agents can read is in private_logic.py and private_host.py.
FRESH = {"Cache-Control": "no-cache"}
import os, asyncio, re, aiohttp
from fleet_web import store
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"
ADMIN, MEMBER = "fleet-private-test@example.com", "fleet-private-member@example.com"
db = store.connect(); pws = {}
for email, admin in ((ADMIN, True), (MEMBER, False)):
    db.execute("DELETE FROM users WHERE email = ?", (email,))
    pws[email] = store.create_user(db, email, is_admin=admin)
    db.execute("UPDATE users SET must_change = 0 WHERE email = ?", (email,))
def ok(n, c, x=""): print(("PASS " if c else "FAIL ") + n + (f"  [{x}]" if x and not c else ""), flush=True)
N = "fleet-test-web-private"

async def login(s, email):
    r = await s.post(B + "/login", data={"email": email, "password": pws[email]}, headers={"Origin": O}, allow_redirects=False)
    H = {"Cookie": "__Host-fleet_session=" + re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)}
    r = await s.get(B + "/settings/account", headers=H)
    return H, re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)

async def main():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), headers=FRESH) as s:
        H, csrf = await login(s, ADMIN); H2, _ = await login(s, MEMBER)
        post = lambda path, data: s.post(B + path, data={"csrf": csrf, **data}, headers={**H, "Origin": O}, allow_redirects=False)
        get = lambda path, h=H: s.get(B + path, headers=h)
        body = await (await get("/projects/new")).text()
        ok("New project offers Private, off by default", 'name="private"' in body and "checked" not in body.split('name="private"')[1][:12])
        r = await post("/projects", {"name": N, "private": "on"})
        ok("create a private project", r.status == 303, r.status)
        body = await (await get(f"/projects/{N}")).text()
        ok("its page says Private and offers Make public", "Private</span>" in body and "Make public" in body)
        body = await (await get("/projects")).text()
        ok("admins see it in the list, marked", N in body and "Private</span>" in body)
        r = await get("/projects", H2); body = await r.text()
        ok("others don't see it in the list or graph", r.status == 200 and N not in body)
        r = await get(f"/projects/{N}", H2)
        ok("... or its page", r.status == 404, r.status)
        r = await get(f"/projects/overview/{N}", H2)
        ok("... or its notes", r.status == 404 or N not in await r.text(), r.status)
        r = await get(f"/search?q={N}", H)
        ok("admins find it in search", f"overview/{N}" in await r.text())
        r = await get(f"/search?q={N}", H2)
        ok("... others don't", f"overview/{N}" not in await r.text())
        r = await post("/agents", {"name": "fleet-test-web-pv", "device": os.environ.get("FLEET_TEST_HUB", "toby"), "type": "shell",
                                   "folder": "~/.cache/fleet-test-pv", "project": N})
        ok("an agent joins it", r.status == 303, r.status)
        body = await (await get("/")).text()
        ok("admins see its project on the agent's card", f"/projects/{N}" in body)
        body = await (await get("/", H2)).text()
        ok("others see the agent, not its project", "fleet-test-web-pv" in body and N not in body)
        r = await post(f"/projects/{N}/private", {"private": "0"})
        body = await r.text()
        ok("Make public works", r.status == 200 and "every agent can read" in body and "Make private" in body, r.status)
        r = await get(f"/projects/{N}", H2)
        ok("public again: others can open it", r.status == 200, r.status)
        r = await post(f"/projects/{N}/private", {"private": "1"})
        ok("Make private works", "only its own agents" in await r.text())
        r = await get(f"/projects/{N}", H2)
        ok("private again: closed to others", r.status == 404, r.status)
        r = await s.post(B + f"/projects/{N}/private", data={"csrf": "x", "private": "0"}, headers={**H2, "Origin": O}, allow_redirects=False)
        ok("others can't switch it", r.status == 403, r.status)
        n = db.execute("SELECT count(*) FROM audit WHERE actor = ? AND action = 'project-private'", (ADMIN,)).fetchone()[0]
        ok("switching is audited", n == 2, n)

try:
    asyncio.run(main())
finally:
    for email in (ADMIN, MEMBER):
        db.execute("DELETE FROM users WHERE email = ?", (email,))
        db.execute("DELETE FROM audit WHERE actor = ? OR detail = ?", (email, email))
