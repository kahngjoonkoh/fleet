# Fleet-web serves pages from a few-seconds-old copy of fleetd's state; the tests change state
# behind its back (straight through fleetd), so they ask for fresh pages, as a hard reload does.
FRESH = {"Cache-Control": "no-cache"}
import os, asyncio, re, aiohttp
from fleet_web import store
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"; E = "fleet-pf-test@example.com"
REMOTE, HUB = os.environ.get("FLEET_TEST_REMOTE", "e4"), os.environ.get("FLEET_TEST_HUB", "toby")
db = store.connect(); db.execute("DELETE FROM users WHERE email = ?", (E,))
pw = store.create_user(db, E, is_admin=True); db.execute("UPDATE users SET must_change = 0 WHERE email = ?", (E,))
def ok(n, c, x=""): print(("PASS " if c else "FAIL ") + n + (f"  [{x}]" if x and not c else ""), flush=True)
async def fleetd(path, body=None):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector("/run/fleet/fleetd.sock")) as s:
        async with (s.post("http://fleetd" + path, json=body) if body is not None else s.get("http://fleetd" + path)) as r:
            return await r.json()
async def project_of(name):
    for d in (await fleetd("/agents"))["devices"]:
        for a in d.get("agents", []):
            if a["name"] == name: return a.get("project")
async def main():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), headers=FRESH) as s:
        r = await s.post(B + "/login", data={"email": E, "password": pw}, headers={"Origin": O}, allow_redirects=False)
        H = {"Cookie": "__Host-fleet_session=" + re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)}
        r = await s.get(B + "/settings/account", headers=H); csrf = re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)
        post = lambda path, data: s.post(B + path, data={"csrf": csrf, **data}, headers={**H, "Origin": O}, allow_redirects=False)
        body = await (await s.get(B + "/agents/new", headers=H)).text()
        existing = [p["name"] for p in (await fleetd("/projects"))["projects"]]
        chips = re.findall(r'name="project" value="([^"]*)"', body)
        ok("Project choices: None, then every project", chips == [""] + existing, chips)
        ok("New project link only when there are none", ('href="/projects/new"' in body) == (not existing))
        r = await post("/agents", {"name": "fleet-test-none", "device": REMOTE, "type": "shell", "folder": "~/.cache/fleet-test-pf", "project": ""})
        ok("create -> back to Agents", r.status == 303 and r.headers["Location"] == "/", r.headers.get("Location"))
        ok("None means no project", await project_of("fleet-test-none") is None)
        await post("/projects", {"name": "fleet-test-pf"})
        ok("a project made later doesn't take agents by folder", await project_of("fleet-test-none") is None)
        await post("/agents", {"name": "fleet-test-picked", "device": REMOTE, "type": "shell", "folder": "~/.cache/fleet-test-pf2",
                               "project": "fleet-test-pf"})
        ok("picking a project puts the agent in it", await project_of("fleet-test-picked") == "fleet-test-pf")
        body = await (await s.get(B + "/agents/new?project=fleet-test-pf", headers=H)).text()
        ok("New agent here pre-selects the project", re.search(r'value="fleet-test-pf"\s+checked', body) is not None)
        body = await (await s.get(B + "/agents/new", headers=H)).text()
        ok("otherwise None is pre-selected", re.search(r'name="project" value=""\s+checked', body) is not None)


try:
    asyncio.run(main())
finally:
    db.execute("DELETE FROM users WHERE email = ?", (E,)); db.execute("DELETE FROM audit WHERE actor = ?", (E,))
