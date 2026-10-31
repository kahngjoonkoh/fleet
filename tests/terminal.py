# Fleet-web serves pages from a few-seconds-old copy of fleetd's state; the tests change state
# behind its back (straight through fleetd), so they ask for fresh pages, as a hard reload does.
FRESH = {"Cache-Control": "no-cache"}
import os, asyncio, re, sys, aiohttp, sqlite3
from fleet_web import store
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"
REMOTE, HUB = os.environ.get("FLEET_TEST_REMOTE", "e4"), os.environ.get("FLEET_TEST_HUB", "toby")
EMAIL = "fleet-term-test@example.com"
db = store.connect()
db.execute("DELETE FROM users WHERE email = ?", (EMAIL,))
pw = store.create_user(db, EMAIL, is_admin=True)
db.execute("UPDATE users SET must_change = 0 WHERE email = ?", (EMAIL,))
def ok(name, cond): print(("PASS " if cond else "FAIL ") + name)
async def main():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), headers=FRESH) as s:
        r = await s.post(B + "/login", data={"email": EMAIL, "password": pw}, headers={"Origin": O}, allow_redirects=False)
        tok = re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)
        H = {"Cookie": f"__Host-fleet_session={tok}"}
        r = await s.get(B + "/terminal", headers=H)
        body = await r.text()
        ok("terminal page 200", r.status == 200)
        ok("terminal CSP allows inline styles only there", "'unsafe-inline'" in r.headers["Content-Security-Policy"])
        r = await s.get(B + "/", headers=H)
        ok("other pages keep strict CSP", "'unsafe-inline'" not in r.headers["Content-Security-Policy"])
        ok("key bar rendered", 'data-mod="ctrl"' in body)
        r = await s.get(B + "/static/vendor/xterm.js"); ok("xterm.js served", r.status == 200)
        try:
            await s.ws_connect(B + "/terminal/ws", headers={**H, "Origin": "https://evil.example"})
            ok("cross-site websocket refused", False)
        except aiohttp.WSServerHandshakeError as e:
            ok("cross-site websocket refused", e.status == 403)
        try:
            await s.ws_connect(B + "/terminal/ws", headers={"Origin": O})
            ok("signed-out websocket refused", False)
        except aiohttp.WSServerHandshakeError as e:
            ok("signed-out websocket refused (redirected to sign-in)", e.status in (200, 303, 401, 403))
        ws = await s.ws_connect(B + "/terminal/ws?session=term-test-0001", headers={**H, "Origin": O})
        await ws.send_str('{"resize": [120, 40]}')
        await ws.send_bytes(b"stty size; whoami; echo MARK-$((6*7))\r")
        out = b""
        try:
            while b"MARK-42" not in out:
                m = await asyncio.wait_for(ws.receive(), 10)
                out += m.data
        except asyncio.TimeoutError:
            pass
        text = out.decode(errors="replace")
        ok("shell runs commands", "MARK-42" in text)
        ok("resize reaches the pty", "40 120" in text)
        ok("shell is kkj", "kkj" in text)
        await ws.send_bytes(b"exit\r")
        m = await asyncio.wait_for(ws.receive(), 10)
        while m.type in (aiohttp.WSMsgType.BINARY,):
            m = await asyncio.wait_for(ws.receive(), 10)
        ok("exit closes the socket", m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING))
asyncio.run(main())
n = db.execute("SELECT count(*) FROM audit WHERE actor = ? AND action = 'terminal-open'", (EMAIL,)).fetchone()[0]
ok("terminal open audited", n >= 1)
db.execute("DELETE FROM users WHERE email = ?", (EMAIL,))
db.execute("DELETE FROM audit WHERE actor = ? OR detail = ?", (EMAIL, EMAIL))
ok("test account removed", db.execute("SELECT count(*) FROM users WHERE email = ?", (EMAIL,)).fetchone()[0] == 0)
