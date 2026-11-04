"""Private projects with real agents and tokens, on the hub and the other device. Runs on the hub, outside
the container, because it reads each agent's token where the agent gets it from."""
import json, os, re, shlex, socket, subprocess, sys, time
HUB, REMOTE = os.environ.get("FLEET_TEST_HUB", socket.gethostname()), os.environ.get("FLEET_TEST_REMOTE", "e4")
STATE = os.path.expanduser("~/.local/state/fleet")
P, Q = "fleet-test-pv", "fleet-test-pv-open"
def ok(n, c, x=""): print(("PASS " if c else "FAIL ") + n + (f"  [{str(x)[:240]}]" if x and not c else ""), flush=True)
def fleetd(path, body=None):
    cmd = ["curl", "-s", "--unix-socket", f"{STATE}/fleetd.sock", "http://fleetd" + path]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout)
def brain(path, tok=None, device=None, form=None, query=None):
    args = ["curl", "-s", "-w", "\n%{http_code}", "--unix-socket", "~/.local/state/fleet/brain.sock"]
    if tok:
        args += ["-H", f"X-Fleet-Token: {tok}"]
    for k, v in (query or {}).items():
        args += ["-G", "--data-urlencode", f"{k}={v}"]
    for k, v in (form or {}).items():
        args += ["--data-urlencode", f"{k}={v}"]
    args.append("http://fleet" + path)
    line = " ".join(a if a.startswith("~") else shlex.quote(a) for a in args)
    p = subprocess.run(["ssh", device, line] if device else ["sh", "-c", line], capture_output=True, text=True)
    text, _, code = p.stdout.rpartition("\n")
    return int(code or 0), text
def token(device, agent):
    cat = f"grep -h '^export FLEET_TOKEN=' ~/.local/state/fleet/agents/{agent}.sh"
    p = subprocess.run(["ssh", device, cat] if device != HUB else ["sh", "-c", cat], capture_output=True, text=True)
    return p.stdout.strip().partition("=")[2].strip("'")

fleetd("/projects/create", {"name": P, "private": True})
fleetd("/projects/create", {"name": Q})
for dev, name, proj, folder in ((HUB, "fleet-test-pv-in", P, "~/.cache/fleet-test-pv"), (REMOTE, "fleet-test-pv-far", P, "~/.cache/fleet-test-pv"),
                                (HUB, "fleet-test-pv-out", Q, "~/.cache/fleet-test-pv-o")):
    r = fleetd("/agents/create", {"device": dev, "name": name, "type": "shell", "folder": folder, "start": True})
    ok(f"agent {name} made", "error" not in r, r)
    r = fleetd("/brain/home", {"device": dev, "agent": name, "project": proj})
    ok(f"agent {name} joined {proj}", "error" not in r, r)
inside, far, outside = token(HUB, "fleet-test-pv-in"), token(REMOTE, "fleet-test-pv-far"), token(HUB, "fleet-test-pv-out")
ok("every agent has its own token in its start script", all((inside, far, outside)) and len({inside, far, outside}) == 3, (inside, far, outside))
ok("each names its agent", inside.startswith(f"{HUB}/fleet-test-pv-in:") and far.startswith(f"{REMOTE}/fleet-test-pv-far:"))
pid = subprocess.run(["systemctl", "--user", "show", "-p", "MainPID", "--value", "fleet-agent@fleet-test-pv-in.service"], capture_output=True, text=True).stdout.strip()
# The unit's main process is the agent's tmux server; what the agent runs is below it.
kids = subprocess.run(["pgrep", "-P", pid], capture_output=True, text=True).stdout.split() if pid not in ("", "0") else []
envs = [open(f"/proc/{k}/environ", "rb").read().decode(errors="replace") for k in kids]
ok("the running agent really has it", any(f"FLEET_TOKEN={inside}" in e for e in envs), (pid, kids))
md = subprocess.run(["ssh", REMOTE, "cat ~/.cache/fleet-test-pv/AGENTS.md"], capture_output=True, text=True).stdout
ok("AGENTS.md tells them to send it", "X-Fleet-Token: $FLEET_TOKEN" in md, md[:100])

for who, tok, dev in (("no token", None, None), ("no token on the other device", None, REMOTE), ("agent in a public project", outside, None),
                      ("a forged token", f"{HUB}/fleet-test-pv-in:{'0' * 40}", None), ("a token for the wrong agent", f"{HUB}/fleet-test-pv-in:" + inside.split(":")[1][::-1], None)):
    t = f"{who}: "
    ok(t + "not in /projects", P + ":" not in brain("/projects", tok, dev)[1] and P + " " not in brain("/projects", tok, dev)[1])
    c, x = brain(f"/p/{P}", tok, dev); ok(t + "project refused", c == 404 and "Project " + P not in x, (c, x))
    c, x = brain(f"/p/{P}/kind/overview", tok, dev); ok(t + "project's kinds refused", c == 404, (c, x))
    c, x = brain(f"/p/{P}/search", tok, dev, query={"q": P}); ok(t + "project search refused", c == 404, (c, x))
    c, x = brain(f"/n/overview/{P}", tok, dev); ok(t + "its note refused", c == 404, (c, x))
    c, x = brain("/search", tok, dev, query={"q": "fleet test pv"}); ok(t + "search doesn't find it", f"overview/{P}:" not in x, x)
    c, x = brain(f"/me/{HUB}/fleet-test-pv-in", tok, dev); ok(t + "that agent's /me doesn't show it", f"# Project {P}" not in x and "no token" in x, x)
    c, x = brain(f"/p/{Q}", tok, dev); ok(t + "public projects stay open", c == 200 and f"# Project {Q}" in x, (c, x))
    c, x = brain("/save", tok, dev, form={"agent": f"{HUB}/fleet-test-pv-in"}); ok(t + "can't save into it", P not in x and "Saved" not in x, x)
    c, x = brain("/save", tok, dev, form={"agent": f"{HUB}/fleet-test-pv-out", "project": P}); ok(t + "can't name it to save", "no project" in x, x)

for who, tok, dev in (("member on the hub", inside, None), ("member on the other device", far, REMOTE)):
    t = f"{who}: "
    ok(t + "listed", P in brain("/projects", tok, dev)[1])
    c, x = brain(f"/p/{P}", tok, dev); ok(t + "reads its project", c == 200 and f"# Project {P}" in x, (c, x))
    c, x = brain(f"/n/overview/{P}", tok, dev); ok(t + "reads its note", c == 200, (c, x))
    c, x = brain("/search", tok, dev, query={"q": "fleet test pv"}); ok(t + "search finds it", f"overview/{P}" in x, x)
    c, x = brain(f"/me/{HUB}/fleet-test-pv-in", tok, dev); ok(t + "/me shows it", f"# Project {P}" in x, x)
    c, x = brain(f"/p/{Q}", tok, dev); ok(t + "freely reads public projects", c == 200 and f"# Project {Q}" in x, (c, x))

c, x = brain("/here", None, None, query={"host": HUB, "folder": os.path.expanduser("~/.cache/fleet-test-pv"), "home": os.path.expanduser("~")})
ok("a session Fleet didn't start, in its folder, doesn't get it", f"# Project {P}" not in x and "No project you can read" in x, x)
r = fleetd("/brain/home", {"device": HUB, "agent": "fleet-test-pv-in", "project": Q})
c, x = brain(f"/p/{P}", inside); ok("moved out of the project: access ends at once, same token", c == 404, (c, x))
r = fleetd("/brain/home", {"device": HUB, "agent": "fleet-test-pv-in", "project": P})
c, x = brain(f"/p/{P}", inside); ok("moved back: access returns", c == 200, (c, x))
r = fleetd("/projects/private", {"name": P, "private": False})
c, x = brain(f"/p/{P}"); ok("made public: anyone reads it", c == 200, (c, x))
r = fleetd("/projects/private", {"name": P, "private": True})
ok("making private names the agents to restart", sorted(r.get("agents", [])) == sorted([f"{HUB}/fleet-test-pv-in", f"{REMOTE}/fleet-test-pv-far"]), r)
c, x = brain(f"/p/{P}"); ok("private again", c == 404, (c, x))
