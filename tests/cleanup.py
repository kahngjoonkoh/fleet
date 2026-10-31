"""Removes everything the tests make, whether they passed or not: agents and projects named
fleet-test-*, notes and rules they produced, and their throwaway accounts. run.sh runs it after
every test. Anything without fleet-test in its name is left alone."""

import asyncio, aiohttp
from fleet_web import store


async def fleetd(path, body=None):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector("/run/fleet/fleetd.sock")) as s:
        async with (s.post("http://fleetd" + path, json=body) if body is not None else s.get("http://fleetd" + path)) as r:
            return await r.json()


async def main():
    removed = []
    for d in (await fleetd("/agents"))["devices"]:
        for a in d.get("agents", []):
            if a["name"].startswith("fleet-test"):
                await fleetd("/agents/remove", {"device": d["name"], "name": a["name"]})
                removed.append(f"agent {d['name']}/{a['name']}")
    for p in (await fleetd("/projects"))["projects"]:
        if p["name"].startswith("fleet-test"):
            await fleetd("/projects/delete", {"name": p["name"]})
            removed.append(f"project {p['name']}")
    # Notes a test agent produced but no project kept: rules, and shared things nothing links to any more.
    rules = (await fleetd("/brain"))["rules"]
    for note_id in rules:
        detail = await brain_note(note_id)
        if "fleet-test" in note_id or (detail and any("fleet-test" in src for src in detail["note"]["sources"])):
            await fleetd("/brain/delete", {"id": note_id})
            removed.append(f"rule {note_id}")
    # Any other note only a test agent wrote (a test's brain update can finish after its project is gone).
    for note_id in await test_notes():
        await fleetd("/brain/delete", {"id": note_id})
        removed.append(f"note {note_id}")
    db = store.connect()
    n = db.execute("DELETE FROM users WHERE email LIKE 'fleet-%test%@example.com'").rowcount
    db.execute("DELETE FROM audit WHERE actor LIKE 'fleet-%test%@example.com' OR detail LIKE '%fleet-test%'")
    if n:
        removed.append(f"{n} test accounts")
    print("cleanup: " + (", ".join(removed) if removed else "nothing left over"))


async def test_notes():
    """Notes whose every source is a fleet-test agent, found through search's view of all notes."""
    found = []
    graph = await fleetd("/brain")
    ids = {n["id"] for n in graph["graph"]["nodes"]} | set(graph["rules"])
    for p in (await fleetd("/projects"))["projects"]:
        ids |= set((await fleetd(f"/projects/{p['name']}"))["notes"])
    for note_id in ids:
        detail = await brain_note(note_id)
        sources = detail["note"]["sources"] if detail else []
        if sources and all("fleet-test" in s for s in sources):
            found.append(note_id)
    return found


async def brain_note(note_id):
    async with aiohttp.ClientSession(connector=aiohttp.UnixConnector("/run/fleet/fleetd.sock")) as s:
        async with s.get("http://fleetd/brain/note", params={"id": note_id}) as r:
            return await r.json() if r.status == 200 else None


asyncio.run(main())
