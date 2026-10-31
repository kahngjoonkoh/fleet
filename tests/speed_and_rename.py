# Fleet-web serves pages from a few-seconds-old copy of fleetd's state; the tests change state
# behind its back (straight through fleetd), so they ask for fresh pages, as a hard reload does.
FRESH = {"Cache-Control": "no-cache"}
import os, asyncio, re, time, aiohttp
from fleet_web import store
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"; E = "fleet-resp-test@example.com"
REMOTE, HUB = os.environ.get("FLEET_TEST_REMOTE", "e4"), os.environ.get("FLEET_TEST_HUB", "toby")
db = store.connect(); db.execute("DELETE FROM users WHERE email = ?", (E,))
pw = store.create_user(db, E, is_admin=True); db.execute("UPDATE users SET must_change = 0 WHERE email = ?", (E,))
def ok(n, c, x=""): print(("PASS " if c else "FAIL ") + n + (f"  [{x}]" if x and not c else ""), flush=True)
async def fleetd(path, body=None):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector("/run/fleet/fleetd.sock")) as s:
        async with (s.post("http://fleetd" + path, json=body) if body is not None else s.get("http://fleetd" + path)) as r:
            return await r.json()
async def agent(name):
    for d in (await fleetd("/agents"))["devices"]:
        for a in d.get("agents", []):
            if a["name"] == name: return a
async def main():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), headers=FRESH) as s:
        r = await s.post(B + "/login", data={"email": E, "password": pw}, headers={"Origin": O}, allow_redirects=False)
        ok("sign-in response not stored", r.headers.get("Cache-Control") == "no-store", r.headers.get("Cache-Control"))
        H = {"Cookie": "__Host-fleet_session=" + re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)}
        r = await s.get(B + "/settings/account", headers=H); csrf = re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)
        post = lambda path, data, **h: s.post(B + path, data={"csrf": csrf, **data}, headers={**H, "Origin": O, **h}, allow_redirects=False)
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("pages: private, no-cache (back/forward can be instant)", r.headers["Cache-Control"] == "private, no-cache", r.headers["Cache-Control"])
        ok("pages carry the Speculation-Rules header", r.headers.get("Speculation-Rules") == '"/speculation-rules.json"')
        ok("no more meta refresh; Agents has a live region", 'http-equiv="refresh"' not in body and 'data-live="agents"' in body)
        css = re.search(r'href="(/static/style\.css\?v=[0-9a-f]{10})"', body)
        ok("static links are versioned", css is not None)
        r = await s.get(B + css.group(1), headers=H); ok("versioned static cached for a year", "max-age=31536000" in r.headers["Cache-Control"], r.headers["Cache-Control"])
        r = await s.get(B + "/static/style.css", headers=H); ok("unversioned static revalidates", r.headers["Cache-Control"] == "no-cache")
        r = await s.get(B + "/speculation-rules.json")
        ok("speculation rules served", r.status == 200 and r.content_type == "application/speculationrules+json" and "/terminal*" in await r.text())
        r = await s.get(B + "/settings/users", headers=H); ok("users page (one-time passwords) not stored", r.headers["Cache-Control"] == "no-store")
        t = time.perf_counter(); await (await s.get(B + "/agents/new", headers=H)).read(); first = time.perf_counter() - t
        t = time.perf_counter(); await (await s.get(B + "/agents/new", headers=H)).read(); second = time.perf_counter() - t
        ok(f"New agent form remembered ({first:.2f}s then {second:.2f}s)", second < 0.2, (first, second))

        await post("/projects", {"name": "fleet-test-rn"})
        await post("/agents", {"name": "fleet-test-r1", "device": REMOTE, "type": "claude", "folder": "~/.cache/fleet-test-rn",
                               "project": "fleet-test-rn", "remote_control": "on"})
        r = await post(f"/agents/{REMOTE}/fleet-test-r1/pin", {}, Referer=O + "/projects/fleet-test-rn")
        ok("a button on a project page returns to that page", r.status == 303 and r.headers["Location"] == "/projects/fleet-test-rn", r.headers.get("Location"))
        r = await post(f"/agents/{REMOTE}/fleet-test-r1/unpin", {}, Referer="https://evil.example/projects/x")
        ok("...but never to another site", r.headers["Location"] == "/", r.headers.get("Location"))
        body = await (await s.get(B + f"/agents/{REMOTE}/fleet-test-r1/edit", headers=H)).text()
        ok("Edit has a name field", 'name="name" value="fleet-test-r1"' in body)
        r = await post(f"/agents/{REMOTE}/fleet-test-r1/edit", {"name": "fleet-test-r2", "project": "fleet-test-rn", "was_rc": "1", "remote_control": "on"})
        a = await agent("fleet-test-r2")
        ok("renamed a running agent", r.status == 303 and a and a["state"] == "running" and await agent("fleet-test-r1") is None, a)
        ok("kept its project and remote control", a and a["project"] == "fleet-test-rn" and a["remote_control"], a and (a["project"], a["remote_control"]))
        r = await post(f"/agents/{REMOTE}/fleet-test-r2/edit", {"name": "bad name!", "project": "fleet-test-rn"})
        ok("bad new name refused", r.status == 400)
        await fleetd("/agents/sleep", {"device": REMOTE, "name": "fleet-test-r2"})
        await post(f"/agents/{REMOTE}/fleet-test-r2/edit", {"name": "fleet-test-r3", "project": "fleet-test-rn"})
        a = await agent("fleet-test-r3")
        ok("renaming an asleep agent keeps it asleep", a and a["state"] == "asleep", a and a["state"])
        await post(f"/agents/{REMOTE}/fleet-test-r3/stop", {}); await post(f"/agents/{REMOTE}/fleet-test-r3/remove", {}); await post("/projects/fleet-test-rn/delete", {})
asyncio.run(main())
db.execute("DELETE FROM users WHERE email = ?", (E,)); db.execute("DELETE FROM audit WHERE actor = ?", (E,))
