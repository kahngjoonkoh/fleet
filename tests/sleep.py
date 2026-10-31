import os, asyncio, json, re, uuid, aiohttp
from fleet_web import store, app as webapp
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"
REMOTE, HUB = os.environ.get("FLEET_TEST_REMOTE", "e4"), os.environ.get("FLEET_TEST_HUB", "toby")
ADMIN, MEMBER = "fleet-sleep-test@example.com", "fleet-sleep-member@example.com"
db = store.connect()
pws = {}
for email, admin in ((ADMIN, True), (MEMBER, False)):
    db.execute("DELETE FROM users WHERE email = ?", (email,))
    pws[email] = store.create_user(db, email, is_admin=admin)
    db.execute("UPDATE users SET must_change = 0 WHERE email = ?", (email,))
original = store.get_setting(db, "sleep_after_minutes", None)
def ok(name, cond, extra=""): print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

async def fleetd(path, body=None):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector("/run/fleet/fleetd.sock")) as s:
        async with (s.post("http://fleetd" + path, json=body) if body is not None else s.get("http://fleetd" + path)) as r:
            return await r.json()

async def agent(name, device=REMOTE):
    for d in (await fleetd("/agents"))["devices"]:
        if d["name"] == device:
            return next((a for a in d.get("agents", []) if a["name"] == name), None)

async def login(s, email):
    r = await s.post(B + "/login", data={"email": email, "password": pws[email]}, headers={"Origin": O}, allow_redirects=False)
    tok = re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)
    H = {"Cookie": f"__Host-fleet_session={tok}"}
    r = await s.get(B + "/settings/account", headers=H)
    return H, re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)

async def open_agent(s, H, name, text, timeout=25):
    ws = await s.ws_connect(B + f"/terminal/ws?device={REMOTE}&agent={name}&session={uuid.uuid4()}", headers={**H, "Origin": O})
    await ws.send_str(json.dumps({"resize": [120, 30]}))
    buf = b""
    try:
        async with asyncio.timeout(timeout):
            while text.encode() not in buf:
                m = await ws.receive()
                if m.type != aiohttp.WSMsgType.BINARY:
                    break
                buf += m.data
    except TimeoutError:
        pass
    await ws.close()
    return text.encode() in buf, buf[-200:]

async def main():
    others = [a["name"] for d in (await fleetd("/agents"))["devices"] for a in d.get("agents", []) if not a["name"].startswith("fleet-test")]
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()) as s:
        H, csrf = await login(s, ADMIN)
        post = lambda path, data: s.post(B + path, data={"csrf": csrf, **data}, headers={**H, "Origin": O}, allow_redirects=False)

        r = await s.get(B + "/settings/agents", headers=H); body = await r.text()
        ok("Settings > Agents 200 with choices", r.status == 200 and "Put idle agents to sleep after" in body and "24 h" in body, r.status)
        r = await post("/settings/agents", {"sleep_after": "60"}); body = await r.text()
        ok("save 1 h", r.status == 200 and "Saved." in body and store.get_setting(db, "sleep_after_minutes", "") == "60")
        r = await post("/settings/agents", {"sleep_after": "7"})
        ok("unlisted time refused", r.status == 400, r.status)
        r = await s.get(B + "/agents/new", headers=H); body = await r.text()
        ok("form: no pin option or hint text", 'name="pinned"' not in body and 'class="hint"' not in body and 'name="boot"' not in body)

        r = await post("/agents", {"name": "fleet-test-l", "device": REMOTE, "type": "claude", "folder": "~"})
        ok("create Claude agent", r.status == 303, r.status)
        a = await agent("fleet-test-l")
        ok("running, lazy, not pinned", a and a["state"] == "running" and a["lazy"] and not a["pinned"], a)
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("card: sleeps after 1 h idle, pin icon", "After 1 h idle" in body and 'aria-label="Pin: never put it to sleep"' in body)

        r = await post(f"/agents/{REMOTE}/fleet-test-l/pin", {})
        a = await agent("fleet-test-l")
        ok("pin", r.status == 303 and a["pinned"], a)
        r = await fleetd("/agents/sleep", {"device": REMOTE, "name": "fleet-test-l"})
        ok("a pinned agent refuses to sleep", "error" in r, r)
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("card: filled pin, Unpin, never sleeps", 'class="pin on"' in body and 'aria-pressed="true"' in body and "Never; pinned" in body)
        await post(f"/agents/{REMOTE}/fleet-test-l/unpin", {})
        ok("unpin", not (await agent("fleet-test-l"))["pinned"])

        if others:
            ok(f"idle sweep skipped: real agents exist {others}", True)
            await fleetd("/agents/sleep", {"device": REMOTE, "name": "fleet-test-l"})
        else:
            await asyncio.sleep(3)
            await webapp.sleep_idle_agents(db, 0.02)  # 1.2 s of quiet is enough here
        a = await agent("fleet-test-l")
        ok("idle agent put to sleep", a and a["state"] == "asleep" and a["memory"] is None, a)
        n = db.execute("SELECT count(*) FROM audit WHERE actor = 'fleet' AND action = 'agent-sleep' AND detail LIKE ?", (f"{REMOTE}/fleet-test-l%",)).fetchone()[0]
        ok("sleep audited", others or n >= 1, n)
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("card: Asleep, wakes when opened, Open link", ">Asleep<" in body and "Wakes when opened" in body and "agent=fleet-test-l" in body)

        got, tail = await open_agent(s, H, "fleet-test-l", "trust")
        a = await agent("fleet-test-l")
        ok("opening wakes it and shows Claude", got and a["state"] == "running", (tail, a))

        await fleetd("/agents/sleep", {"device": REMOTE, "name": "fleet-test-l"})
        await post(f"/agents/{REMOTE}/fleet-test-l/pin", {})
        a = await agent("fleet-test-l")
        ok("pinning an asleep agent wakes it", a["state"] == "running" and a["pinned"], a)

        await post("/agents", {"name": "fleet-test-sh", "device": REMOTE, "type": "shell", "folder": "~"})
        r = await fleetd("/agents/sleep", {"device": REMOTE, "name": "fleet-test-sh"})
        a = await agent("fleet-test-sh")
        ok("a shell never sleeps", "error" in r and not a["lazy"], r)
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("shell card: never, can't resume", "Never; it can&#39;t resume" in body or "Never; it can't resume" in body)

        for n in ("fleet-test-l", "fleet-test-sh"):
            await post(f"/agents/{REMOTE}/{n}/stop", {})
            ok(f"{n}: stop -> Stopped", (await agent(n))["state"] == "stopped")
            got, tail = await open_agent(s, H, n, "isn't running", timeout=10)
            ok(f"{n}: opening a stopped agent doesn't wake it", got and (await agent(n))["state"] == "stopped", tail)
            await post(f"/agents/{REMOTE}/{n}/remove", {})
            ok(f"{n}: removed", await agent(n) is None)

        H2, csrf2 = await login(s, MEMBER)
        r = await s.get(B + "/settings/agents", headers=H2); ok("member can't open agent settings", r.status == 403, r.status)

asyncio.run(main())
if original is None:
    db.execute("DELETE FROM settings WHERE key = 'sleep_after_minutes'")
else:
    store.set_setting(db, "sleep_after_minutes", original)
ok("setting restored", store.get_setting(db, "sleep_after_minutes", None) == original)
for email in (ADMIN, MEMBER):
    db.execute("DELETE FROM users WHERE email = ?", (email,))
    db.execute("DELETE FROM audit WHERE actor = ? OR detail = ?", (email, email))
db.execute("DELETE FROM audit WHERE actor = 'fleet' AND detail LIKE '%/fleet-test-%'")
ok("test accounts removed", db.execute("SELECT count(*) FROM users WHERE email IN (?, ?)", (ADMIN, MEMBER)).fetchone()[0] == 0)
