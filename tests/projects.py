import os, asyncio, re, aiohttp
from fleet_web import store
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"; E = "fleet-mf-test@example.com"
REMOTE, HUB = os.environ.get("FLEET_TEST_REMOTE", "e4"), os.environ.get("FLEET_TEST_HUB", "toby")
TF = "~/.cache/fleet-test-mf-hub"
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
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        r = await s.post(B + "/login", data={"email": E, "password": pw}, headers={"Origin": O}, allow_redirects=False)
        H = {"Cookie": "__Host-fleet_session=" + re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)}
        r = await s.get(B + "/settings/account", headers=H); csrf = re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)
        post = lambda path, data: s.post(B + path, data={"csrf": csrf, **data}, headers={**H, "Origin": O}, allow_redirects=False)
        text = lambda path: s.get(B + path, headers=H)
        body = await (await text("/projects/new")).text()
        ok("New project asks only for a name", 'name="name"' in body and 'name="device"' not in body and 'name="folder"' not in body)
        r = await post("/projects", {"name": "fleet-test-m"}); ok("create project", r.status == 303 and r.headers["Location"] == "/projects/fleet-test-m", r.status)
        r = await post("/agents", {"name": "fleet-test-m1", "device": REMOTE, "type": "shell", "folder": "~/.cache/fleet-test-mf", "project": "fleet-test-m"})
        r2 = await post("/agents", {"name": "fleet-test-m2", "device": HUB, "type": "shell", "folder": TF, "project": "fleet-test-m"})
        ok("two agents on two devices, different folders, one project",
           r.status == r2.status == 303 and await project_of("fleet-test-m1") == await project_of("fleet-test-m2") == "fleet-test-m")
        body = await (await text("/projects/fleet-test-m")).text()
        ok("project page lists both", "fleet-test-m1" in body and "fleet-test-m2" in body and "2 agents" in body)
        body = await (await text("/agents/new?project=fleet-test-m")).text()
        ok("New agent here pre-selects it", re.search(r'value="fleet-test-m"\s+checked', body) is not None)
        r = await post(f"/agents/{REMOTE}/fleet-test-m1/edit", {"project": ""})
        ok("moved out via Edit", r.status == 303 and await project_of("fleet-test-m1") is None)
        await post(f"/agents/{REMOTE}/fleet-test-m1/edit", {"project": "fleet-test-m"})
        ok("and back", await project_of("fleet-test-m1") == "fleet-test-m")
        r = await post("/projects/fleet-test-m/instructions", {}); body = await r.text()
        ok("update agents' AGENTS.md: already up to date", r.status == 200 and "already up to date" in body, re.sub(r"\s+", " ", body[body.find("notice"):][:200]))
asyncio.run(main())
db.execute("DELETE FROM users WHERE email = ?", (E,)); db.execute("DELETE FROM audit WHERE actor = ?", (E,))
