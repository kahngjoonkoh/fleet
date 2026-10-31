"""The brain end to end, with a real Claude Code agent: one conversation's notes land in the right
projects, a rule for every agent comes out of it, and search, notes and /me show them. It sends one
message to Claude and runs the writer once or twice, so it uses some of the hub's Claude plan; run.sh
only runs it when asked (./run.sh --paid)."""

import os, asyncio, json, re, uuid, aiohttp
from fleet_web import store
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"; E = "fleet-brain-test@example.com"
REMOTE = os.environ.get("FLEET_TEST_REMOTE", "e4")
db = store.connect(); db.execute("DELETE FROM users WHERE email = ?", (E,))
pw = store.create_user(db, E, is_admin=True); db.execute("UPDATE users SET must_change = 0 WHERE email = ?", (E,))
def ok(n, c, x=""): print(("PASS " if c else "FAIL ") + n + (f"  [{x}]" if x and not c else ""), flush=True)
FOLDER = "~/.cache/fleet-test-a"


async def fleetd(path, body=None):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector("/run/fleet/fleetd.sock")) as s:
        async with (s.post("http://fleetd" + path, json=body) if body is not None else s.get("http://fleetd" + path)) as r:
            return await r.json() if r.content_type == "application/json" else await r.text()


async def main():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        r = await s.post(B + "/login", data={"email": E, "password": pw}, headers={"Origin": O}, allow_redirects=False)
        H = {"Cookie": "__Host-fleet_session=" + re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)}
        r = await s.get(B + "/settings/account", headers=H); csrf = re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)
        post = lambda path, data: s.post(B + path, data={"csrf": csrf, **data}, headers={**H, "Origin": O}, allow_redirects=False)
        text = lambda path: s.get(B + path, headers=H)

        for name in ("fleet-test-a", "fleet-test-b"):
            r = await post("/projects", {"name": name}); ok(f"project {name}", r.status == 303, r.status)
        r = await post("/rules", {"title": "Never restart the fleet test tunnel without asking", "body": "Test rule."})
        ok("rule added by hand", r.status == 303 and "Never restart the fleet test tunnel" in await (await text("/projects")).text())
        r = await post("/agents", {"name": "fleet-test-a1", "device": REMOTE, "type": "claude", "folder": FOLDER,
                                   "project": "fleet-test-a", "remote_control": "off"})
        ok("Claude agent in project A", r.status == 303, r.status)

        ws = await s.ws_connect(B + f"/terminal/ws?device={REMOTE}&agent=fleet-test-a1&session={uuid.uuid4()}", headers={**H, "Origin": O})
        await ws.send_str(json.dumps({"resize": [120, 40]}))

        async def screen(sec):
            buf = b""
            try:
                async with asyncio.timeout(sec):
                    while True:
                        m = await ws.receive()
                        if m.type != aiohttp.WSMsgType.BINARY:
                            break
                        buf += m.data
            except TimeoutError:
                pass
            return buf.decode(errors="replace")

        await screen(6)
        await ws.send_bytes(b"\x1b[B"); await screen(1); await ws.send_bytes(b"\r"); await screen(6)  # trust the folder
        await ws.send_bytes(b"Three things to remember, no need to look anything up. 1) In fleet-test-a we chose Redis over "
                            b"Memcached for the job queue, because Redis persists jobs across restarts. 2) For project fleet-test-b: "
                            b"its nightly export must run at 03:00 UTC; that is a requirement of fleet-test-b, not of this project. "
                            b"3) A rule for every agent in all projects: never run database migrations without asking the user first. "
                            b"Reply with just: noted")
        await screen(2); await ws.send_bytes(b"\r")
        out = await screen(70)
        await ws.close()
        ok("agent answered", "noted" in out.lower(), out[-200:])

        m = None
        for _ in range(4):
            body = await (await post("/projects/fleet-test-a/digest", {})).text()
            m = re.search(r"Read (\d+) new slices[^<]*", body)
            if m and int(m.group(1)):
                break
            await asyncio.sleep(15)
        ok("brain updated from the conversation", m is not None and int(m.group(1)) > 0, m.group(0) if m else body[:300])

        a = (await fleetd("/projects/fleet-test-a"))["notes"]
        b = (await fleetd("/projects/fleet-test-b"))["notes"]
        rules = (await fleetd("/brain"))["rules"]
        ok("A has the Redis decision", any(n["kind"] == "decision" and "redis" in n["title"].lower() for n in a.values()), list(a))
        ok("B got its own requirement from A's agent", any("export" in n["title"].lower() or "03:00" in n["title"] for n in b.values()), list(b))
        ok("the migrations rule is a rule for every agent", any("migration" in n["title"].lower() for n in rules.values()), list(rules))
        ok("only requirements and conventions are rules", {i.split("/")[0] for i in rules} <= {"requirement", "convention"}, list(rules))
        res = await (await text("/search?q=redis")).text()
        ok("search across projects, labelled", "fleet-test-a" in res and "/notes/decision/" in res)
        nid = next(i for i, n in a.items() if n["kind"] == "decision")
        r = await post(f"/notes/{nid}/edit", {"title": "Redis for the job queue", "status": "superseded", "scope": "project",
                                              "projects": "fleet-test-a", "body": "Edited by hand."})
        page = await (await text(f"/notes/{nid}")).text()
        ok("note edited by hand", r.status == 303 and "Edited by hand." in page and "superseded" in page, r.status)


try:
    asyncio.run(main())
finally:
    db.execute("DELETE FROM users WHERE email = ?", (E,))
    db.execute("DELETE FROM audit WHERE actor = ?", (E,))
