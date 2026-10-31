# Fleet-web serves pages from a few-seconds-old copy of fleetd's state; the tests change state
# behind its back (straight through fleetd), so they ask for fresh pages, as a hard reload does.
FRESH = {"Cache-Control": "no-cache"}
import os, asyncio, json, re, uuid, aiohttp
from fleet_web import store
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"
REMOTE, HUB = os.environ.get("FLEET_TEST_REMOTE", "e4"), os.environ.get("FLEET_TEST_HUB", "toby")
ADMIN, MEMBER = "fleet-agents-test@example.com", "fleet-agents-member@example.com"
db = store.connect()
pws = {}
for email, admin in ((ADMIN, True), (MEMBER, False)):
    db.execute("DELETE FROM users WHERE email = ?", (email,))
    pws[email] = store.create_user(db, email, is_admin=admin)
    db.execute("UPDATE users SET must_change = 0 WHERE email = ?", (email,))
def ok(name, cond, extra=""): print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

async def fleetd(path):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector("/run/fleet/fleetd.sock")) as s:
        async with s.get("http://fleetd" + path) as r:
            return await r.json()

async def agent(name, device):
    for d in (await fleetd("/agents"))["devices"]:
        if d["name"] == device:
            return next((a for a in d.get("agents", []) if a["name"] == name), None)

async def screen_has(s, H, device, name, text, send=None, timeout=12):
    ws = await s.ws_connect(B + f"/terminal/ws?device={device}&agent={name}&session={uuid.uuid4()}", headers={**H, "Origin": O})
    await ws.send_str(json.dumps({"resize": [120, 30]}))
    if send:
        await asyncio.sleep(1.5)
        await ws.send_bytes(send.encode())
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
    return text.encode() in buf, buf[-300:]

async def login(s, email):
    r = await s.post(B + "/login", data={"email": email, "password": pws[email]}, headers={"Origin": O}, allow_redirects=False)
    tok = re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)
    H = {"Cookie": f"__Host-fleet_session={tok}"}
    r = await s.get(B + "/settings/account", headers=H)
    csrf = re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)
    return H, csrf

async def main():
    opts = (await fleetd("/agents/options"))["devices"]
    hub = next(d["name"] for d in opts if d["local"])
    ok("options from every device", {d["name"] for d in opts} >= {hub, REMOTE} and all("error" not in d for d in opts), opts)
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), headers=FRESH) as s:
        H, csrf = await login(s, ADMIN)
        post = lambda path, data: s.post(B + path, data={"csrf": csrf, **data}, headers={**H, "Origin": O}, allow_redirects=False)
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("Agents page 200", r.status == 200, r.status)
        ok("New agent button for admin", 'href="/agents/new"' in body)
        r = await s.get(B + "/agents/new", headers=H); body = await r.text()
        ok("New agent form 200", r.status == 200, r.status)
        ok("form lists both devices", f'value="{hub}"' in body and f'value="{REMOTE}"' in body)
        ok("form offers Claude Code, Shell, Custom", all(x in body for x in ("Claude Code", ">Shell<", "Custom command")))
        ok("folder suggestions present", "<datalist" in body and "~/dev/fleet" in body)
        r = await post("/agents", {"name": "bad name!", "device": REMOTE, "type": "shell", "folder": "~"})
        ok("bad name refused", r.status == 400, r.status)
        r = await post("/agents", {"name": "fleet-test-x", "device": REMOTE, "type": "shell", "folder": "~/no-such-folder-zz"})
        ok("missing folder refused", r.status == 400 and "no folder" in await r.text(), r.status)
        r = await post("/agents", {"name": "fleet-test-x", "device": REMOTE, "type": "custom", "folder": "~"})
        ok("custom without command refused", r.status == 400, r.status)
        for device in (REMOTE, hub):
            r = await post("/agents", {"name": "fleet-test-a", "device": device, "type": "shell", "folder": "~"})
            ok(f"create on {device} -> back to Agents", r.status == 303 and r.headers.get("Location") == "/",
               (r.status, r.headers.get("Location")))
        r = await post("/agents", {"name": "fleet-test-a", "device": REMOTE, "type": "shell", "folder": "~"})
        ok("duplicate name refused", r.status == 400 and "already" in await r.text(), r.status)
        r = await s.get(B + f"/terminal?device={REMOTE}&agent=fleet-test-a", headers=H); body = await r.text()
        ok("terminal page for agent", r.status == 200 and 'data-agent="fleet-test-a"' in body and f"on {REMOTE}" in body)
        for device in (REMOTE, hub):
            a = await agent("fleet-test-a", device)
            ok(f"{device}: listed running", a and a["state"] == "running", a)
            got, tail = await screen_has(s, H, device, "fleet-test-a", "FLEET2", send="echo FLEET$((1+1))\r")
            ok(f"{device}: typing reaches the agent", got, tail)
            got, tail = await screen_has(s, H, device, "fleet-test-a", "FLEET2")
            ok(f"{device}: reattaching from a new tab shows the same screen", got, tail)
        await screen_has(s, H, REMOTE, "fleet-test-a", "zzz-never", send="exit 7\r", timeout=4)
        await asyncio.sleep(1)
        a = await agent("fleet-test-a", REMOTE)
        ok(f"{REMOTE}: exit leaves it Exited with its code", a and a["state"] == "exited" and a["pane"]["exit_code"] == 7, a)
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("card shows Exited · code 7", "Exited · code 7" in body)
        r = await post(f"/agents/{REMOTE}/fleet-test-a/start", {})
        ok("restart", r.status == 303, r.status)
        a = await agent("fleet-test-a", REMOTE)
        ok(f"{REMOTE}: running again", a and a["state"] == "running", a)
        for device in (REMOTE, hub):
            r = await post(f"/agents/{device}/fleet-test-a/stop", {})
            a = await agent("fleet-test-a", device)
            ok(f"{device}: stop -> Stopped", r.status == 303 and a and a["state"] == "stopped", a)
            got, tail = await screen_has(s, H, device, "fleet-test-a", "isn't running", timeout=8)
            ok(f"{device}: opening a stopped agent says so", got, tail)
            r = await post(f"/agents/{device}/fleet-test-a/remove", {})
            ok(f"{device}: removed", r.status == 303 and await agent("fleet-test-a", device) is None)
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("tmux sessions Fleet didn't start aren't listed", "Other tmux sessions" not in body)

        H2, csrf2 = await login(s, MEMBER)
        r = await s.get(B + "/", headers=H2); body = await r.text()
        ok("member sees Agents, no New agent", r.status == 200 and 'href="/agents/new"' not in body)
        r = await s.get(B + "/agents/new", headers=H2); ok("member can't open the form", r.status == 403, r.status)
        r = await s.post(B + "/agents", data={"csrf": csrf2, "name": "x1", "device": REMOTE, "type": "shell", "folder": "~"},
                         headers={**H2, "Origin": O}, allow_redirects=False)
        ok("member can't create", r.status == 403, r.status)

asyncio.run(main())
n = db.execute("SELECT count(*) FROM audit WHERE actor = ? AND action LIKE 'agent-%'", (ADMIN,)).fetchone()[0]
ok("agent actions audited", n >= 6, n)
for email in (ADMIN, MEMBER):
    db.execute("DELETE FROM users WHERE email = ?", (email,))
    db.execute("DELETE FROM audit WHERE actor = ? OR detail = ?", (email, email))
ok("test accounts removed", db.execute("SELECT count(*) FROM users WHERE email IN (?, ?)", (ADMIN, MEMBER)).fetchone()[0] == 0)
