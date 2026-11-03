# Files tab, on the hub and the other device: list, preview, download (ranges too), zip, upload,
# edit (with the changed-since-opened check), new folder, rename, delete, and who may do it.
FRESH = {"Cache-Control": "no-cache"}
import os, asyncio, io, re, zipfile, aiohttp
from urllib.parse import urlencode
from fleet_web import store
O = os.environ["FLEET_PUBLIC_ORIGIN"]; B = "http://127.0.0.1:8080"
REMOTE, HUB = os.environ.get("FLEET_TEST_REMOTE", "e4"), os.environ.get("FLEET_TEST_HUB", "toby")
ADMIN, MEMBER = "fleet-files-test@example.com", "fleet-files-member@example.com"
db = store.connect()
pws = {}
for email, admin in ((ADMIN, True), (MEMBER, False)):
    db.execute("DELETE FROM users WHERE email = ?", (email,))
    pws[email] = store.create_user(db, email, is_admin=admin)
    db.execute("UPDATE users SET must_change = 0 WHERE email = ?", (email,))
def ok(name, cond, extra=""): print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""), flush=True)
DIR = "~/.cache/fleet-test-files"


async def login(s, email):
    r = await s.post(B + "/login", data={"email": email, "password": pws[email]}, headers={"Origin": O}, allow_redirects=False)
    H = {"Cookie": "__Host-fleet_session=" + re.search(r"__Host-fleet_session=([^;]+)", r.headers["Set-Cookie"]).group(1)}
    r = await s.get(B + "/settings/account", headers=H)
    return H, re.search(r'name="csrf" value="([^"]+)"', await r.text()).group(1)


def url(view, device, path, **extra):
    return f"{B}/files{'/' + view if view else ''}?" + urlencode({"device": device, "path": path, **extra})


async def main():
    async with aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar(), headers=FRESH) as s:
        H, csrf = await login(s, ADMIN)
        post = lambda path, data: s.post(B + path, data={"csrf": csrf, **data}, headers={**H, "Origin": O}, allow_redirects=False)
        write = lambda device, path, body, **q: s.post(B + "/files/write?" + urlencode({"device": device, "path": path, **q}), data=body,
                                                       headers={**H, "Origin": O, "X-Fleet-CSRF": csrf})
        r = await s.get(B + "/", headers=H); body = await r.text()
        ok("nav has Files; agent cards link to Files", 'href="/files"' in body)

        for dev in (HUB, REMOTE):
            t = f"[{dev}] "
            r = await s.get(url("", dev, DIR), headers=H); body = await r.text()
            ok(t + "empty folder lists", r.status == 200 and "This folder is empty" in body, r.status)

            r = await write(dev, f"{DIR}/hello.txt", b"hello\nworld\n")
            j = await r.json()
            ok(t + "upload", r.status == 200 and j.get("path") == f"{DIR}/hello.txt" and j.get("size") == 12, j)
            r = await write(dev, f"{DIR}/hello.txt", b"second")
            j = await r.json()
            ok(t + "an upload never replaces a file: saved as hello (1).txt", j.get("path") == f"{DIR}/hello (1).txt", j)
            r = await s.post(B + "/files/write?" + urlencode({"device": dev, "path": f"{DIR}/x.txt"}), data=b"x",
                             headers={**H, "Origin": O, "X-Fleet-CSRF": "wrong"})
            ok(t + "upload without the csrf token refused", r.status == 403, r.status)

            big = os.urandom(300_000)
            r = await write(dev, f"{DIR}/blob.bin", big)
            ok(t + "binary upload, 300 KB", r.status == 200, await r.text())
            r = await s.get(url("raw", dev, f"{DIR}/blob.bin", dl="1"), headers=H)
            data = await r.read()
            ok(t + "download matches, as an attachment", data == big and r.headers["Content-Disposition"].startswith("attachment")
               and r.headers["Content-Type"] == "application/octet-stream")
            r = await s.get(url("raw", dev, f"{DIR}/blob.bin"), headers={**H, "Range": "bytes=1000-1999"})
            ok(t + "range request (video seeking)", r.status == 206 and await r.read() == big[1000:2000]
               and r.headers["Content-Range"] == f"bytes 1000-1999/{len(big)}", r.status)

            await write(dev, f"{DIR}/page.html", b"<script>alert(1)</script>")
            r = await s.get(url("raw", dev, f"{DIR}/page.html"), headers=H)
            ok(t + "HTML is never served as a page: plain text, sandboxed", r.headers["Content-Type"].startswith("text/plain")
               and r.headers["Content-Security-Policy"].startswith("sandbox"), dict(r.headers))
            await write(dev, f"{DIR}/pic.svg", b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>')
            r = await s.get(url("raw", dev, f"{DIR}/pic.svg"), headers=H)
            ok(t + "SVG shown only under a sandbox", r.headers["Content-Type"] == "image/svg+xml"
               and r.headers["Content-Security-Policy"].startswith("sandbox"))

            r = await s.get(url("", dev, DIR), headers=H); body = await r.text()
            ok(t + "listing shows the files, sizes and download links", all(n in body for n in ("hello.txt", "hello (1).txt", "blob.bin"))
               and "293 KB" in body and "dl=1" in body)
            r = await s.get(url("view", dev, f"{DIR}/hello.txt"), headers=H); body = await r.text()
            ok(t + "text preview + Edit", "hello\nworld" in body and "/files/edit?" in body)
            await write(dev, f"{DIR}/notes.md", b"# Title\n\n- **bold** item\n")
            r = await s.get(url("view", dev, f"{DIR}/notes.md"), headers=H); body = await r.text()
            ok(t + "markdown rendered", ">Title</h" in body and "<strong>bold</strong>" in body)
            await write(dev, f"{DIR}/t.csv", b"a,b\n1,2\n")
            r = await s.get(url("view", dev, f"{DIR}/t.csv"), headers=H); body = await r.text()
            ok(t + "CSV as a table", "<th>a</th>" in body and "<td>2</td>" in body)
            r = await s.get(url("view", dev, f"{DIR}/blob.bin"), headers=H); body = await r.text()
            ok(t + "binary: no preview, Download", "No preview" in body)

            r = await s.get(url("edit", dev, f"{DIR}/hello.txt"), headers=H); body = await r.text()
            mtime = re.search(r'data-mtime="(\d+)"', body).group(1)
            ok(t + "editor opens with the text", "hello\nworld" in body)
            r = await write(dev, f"{DIR}/hello.txt", b"edited\n", overwrite="1", mtime=mtime)
            ok(t + "save from the editor", r.status == 200, await r.text())
            r = await s.get(url("raw", dev, f"{DIR}/hello.txt"), headers=H)
            ok(t + "saved text is there", await r.read() == b"edited\n")
            r = await write(dev, f"{DIR}/hello.txt", b"stale\n", overwrite="1", mtime=str(int(mtime) - 100))
            ok(t + "save refused when the file changed since it was opened", r.status == 400 and "changed since" in (await r.json())["error"])

            r = await post("/files/mkdir", {"device": dev, "path": DIR, "name": "sub"})
            ok(t + "new folder", r.status == 303)
            await write(dev, f"{DIR}/sub/inner.txt", b"inside")
            r = await post("/files/rename", {"device": dev, "path": f"{DIR}/sub", "name": "sub2"})
            ok(t + "rename a folder", r.status == 303 and "sub2" in r.headers["Location"], r.headers.get("Location"))
            r = await post("/files/rename", {"device": dev, "path": f"{DIR}/hello (1).txt", "name": "../escape.txt"})
            ok(t + "names with / refused", r.status == 400)

            r = await s.get(url("zip", dev, DIR), headers=H)
            z = zipfile.ZipFile(io.BytesIO(await r.read()))
            names = z.namelist()
            ok(t + "folder as zip", r.status == 200 and "fleet-test-files/sub2/inner.txt" in names
               and z.read("fleet-test-files/hello.txt") == b"edited\n", names)

            r = await post("/files/delete", {"device": dev, "path": f"{DIR}/sub2"})
            ok(t + "delete a folder", r.status == 303)
            r = await post("/files/delete", {"device": dev, "path": "~"})
            ok(t + "deleting the home folder refused", r.status == 400)
            r = await s.get(url("", dev, DIR), headers=H); body = await r.text()
            ok(t + "deleted folder gone", "sub2" not in body)
            # A big upload, in pieces: retries, resuming, a dropped connection, never a half file.
            data = os.urandom(3_000_000)
            big_path = f"{DIR}/big.bin"
            step = lambda path, sid, st, body=b"", **q: write(dev, path, body, upload=sid, step=st, total=str(q.pop("total", len(data))), **q)
            j = await (await step(big_path, "t1", "start")).json()
            ok(t + "pieces: a new upload starts at 0", j == {"have": 0}, j)
            j = await (await step(big_path, "t1", "piece", data[:1_000_000], offset="0")).json()
            ok(t + "pieces: first piece", j == {"have": 1_000_000}, j)
            j = await (await step(big_path, "t1", "start")).json()
            ok(t + "pieces: asking again says how far it got (resume)", j == {"have": 1_000_000}, j)
            j = await (await step(big_path, "t1", "piece", data[:1_000_000], offset="0")).json()
            ok(t + "pieces: a retried piece is not added twice", j == {"have": 1_000_000}, j)
            j = await (await step(big_path, "t1", "piece", data[2_000_000:], offset="2000000")).json()
            ok(t + "pieces: a gap isn't written, it says where to continue", j == {"have": 1_000_000} and "path" not in j, j)
            reader, writer = await asyncio.open_connection("127.0.0.1", 8080)
            q = urlencode({"device": dev, "path": big_path, "upload": "t1", "step": "piece", "total": len(data), "offset": 1_000_000})
            writer.write(f"POST /files/write?{q} HTTP/1.1\r\nHost: x\r\nOrigin: {O}\r\nCookie: {H['Cookie']}\r\nX-Fleet-CSRF: {csrf}\r\n"
                         f"Content-Length: 1000000\r\n\r\n".encode() + data[1_000_000:1_400_000])
            await writer.drain(); await asyncio.sleep(1); writer.transport.abort()
            await asyncio.sleep(1)
            r = await s.get(url("", dev, DIR), headers=H); body = await r.text()
            ok(t + "pieces: no half file shows while it's going", "big.bin" not in body)
            r = await s.get(url("zip", dev, DIR), headers=H)
            ok(t + "pieces: unfinished uploads stay out of a zip", not any("fleet-upload" in n for n in zipfile.ZipFile(io.BytesIO(await r.read())).namelist()))
            j = await (await step(big_path, "t1", "start")).json()
            have = j.get("have", -1)
            ok(t + "pieces: after a dropped connection it keeps what arrived", 1_000_000 <= have <= 1_400_000, j)
            j = await (await step(big_path, "t1", "piece", data[have:], offset=str(have))).json()
            ok(t + "pieces: the last one finishes the file", j.get("done") and j.get("path") == big_path and j.get("size") == len(data), j)
            r = await s.get(url("raw", dev, big_path), headers=H)
            ok(t + "pieces: the file is exactly what was sent", await r.read() == data)
            r = await step(big_path, "t2", "piece", b"x" * 10, offset="0", total=5)
            ok(t + "pieces: more than announced refused", r.status == 400 and "bigger than announced" in (await r.json())["error"])
            j = await (await step(big_path, "t3", "piece", b"", offset="0", total=0)).json()
            ok(t + "pieces: empty file; a taken name stays taken", j.get("path") == f"{DIR}/big (1).bin" and j.get("size") == 0, j)
            await step(big_path, "t4", "piece", data[:1000], offset="0")
            await step(big_path, "t4", "cancel")
            j = await (await step(big_path, "t4", "start")).json()
            ok(t + "pieces: cancel throws it away", j == {"have": 0}, j)
            r = await step(big_path, "../../x", "start")
            ok(t + "pieces: a bad id is refused", r.status == 400, r.status)
            r = await step(big_path, "t5", "start", total=10 ** 18)
            ok(t + "pieces: not enough disk says so up front", r.status == 400 and "Not enough space" in (await r.json())["error"])
            n = db.execute("SELECT count(*) FROM audit WHERE actor = ? AND action = 'file-upload' AND detail LIKE ?", (ADMIN, f"{dev}:%big%")).fetchone()[0]
            ok(t + "pieces: audited once per finished file", n == 2, n)

            r = await s.get(url("", dev, "~/.cache/fleet-test-nope"), headers=H)
            ok(t + "missing folder: 404 with a way home", r.status == 404 and "Go to ~" in await r.text(), r.status)

        n = db.execute("SELECT count(*) FROM audit WHERE actor = ? AND action LIKE 'file-%'", (ADMIN,)).fetchone()[0]
        ok("uploads, edits, downloads and changes audited", n >= 20, n)

        H2, csrf2 = await login(s, MEMBER)
        r = await s.get(url("", HUB, "~"), headers=H2)
        ok("members can't open Files", r.status == 403, r.status)
        r = await s.get(url("raw", HUB, "~/.bashrc"), headers=H2)
        ok("... or download", r.status == 403, r.status)


try:
    asyncio.run(main())
finally:
    for email in (ADMIN, MEMBER):
        db.execute("DELETE FROM users WHERE email = ?", (email,))
        db.execute("DELETE FROM audit WHERE actor = ? OR detail = ?", (email, email))
