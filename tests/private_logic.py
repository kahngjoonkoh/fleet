"""The private-project rule itself, on a throwaway database: who can read what through each way of reading,
and what the writer may do. Run by run.sh with FLEET_DATA_DIR pointing somewhere empty."""
import os, sys
assert os.environ.get("FLEET_DATA_DIR"), "needs an empty FLEET_DATA_DIR"
from fleet import brain as b

def ok(name, cond, extra=""): print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""), flush=True)

b.create("pub"); b.create("sec", private=True); b.create("other", private=True)
b.set_home("h/p", "pub"); b.set_home("h/s", "sec"); b.set_home("h/o", "other")
b.upsert("decision/open", "decision", "Open zebra plan", body="public zebra", projects=["pub"])
b.upsert("decision/secret", "decision", "Secret zebra plan", body="classified zebra", projects=["sec"],
         links=[("relates_to", "decision/open")])
b.upsert("tech/both", "tech", "Both zebra tool", body="shared zebra", projects=["pub", "sec"])
b.upsert("tech/hidden", "tech", "Hidden zebra tool", body="hidden zebra", projects=["sec"])
b.upsert("requirement/rule", "requirement", "Global zebra rule", body="rule zebra", scope="global", projects=["sec"])
b.upsert("decision/open", "decision", "Open zebra plan", links=[("relates_to", "decision/secret")])
out, member, third = b.Viewer(), b.Viewer("sec"), b.Viewer("other")

for who, v in (("no token", out), ("public agent", b.Viewer("pub")), ("other private project's agent", third)):
    t = f"{who}: "
    s = b.read_search("zebra", viewer=v)
    ok(t + "search hides private notes", "decision/secret" not in s and "tech/hidden" not in s and "classified" not in s, s)
    ok(t + "search still finds public notes", "decision/open" in s and "tech/both" in s, s)
    ok(t + "a note in both doesn't name the private project", "sec" not in s.split("tech/both")[1].split("\n")[0], s)
    ok(t + "rules still show", "requirement/rule" in s and "Global zebra rule" in b.read_rules(), s)
    ok(t + "projects list hides it", "sec" not in b.read_projects(v) and "pub" in b.read_projects(v))
    for name, fn in (("note", lambda: b.read_note("decision/secret", viewer=v)), ("project", lambda: b.read_project("sec", viewer=v)),
                     ("kind", lambda: b.read_kind("sec", "decision", viewer=v)), ("overview", lambda: b.read_note("overview/sec", viewer=v))):
        try:
            fn(); ok(t + f"reading the {name} refused", False)
        except b.Problem as e:
            ok(t + f"reading the {name} refused like one that doesn't exist", "No note" in str(e) or "no project" in str(e), e)
    n = b.read_note("decision/open", 2, viewer=v)
    ok(t + "links and titles to private notes are left out", "decision/secret" not in n and "Secret" not in n, n)
    ok(t + "no path through or to a private note", "No chain" in b.read_path("decision/open", "decision/secret", viewer=v))
    me = b.read_me("h/s", "sec", v)
    ok(t + "/me of a private agent shows rules, not its project", "Secret" not in me and "# Project sec" not in me and "no token" in me, me)
    ok(t + "public project stays readable", "Open zebra plan" in b.read_project("pub", viewer=v))

ok("member: search finds its private notes", "decision/secret" in b.read_search("zebra", viewer=member))
ok("member: reads its project", "Secret zebra plan" in b.read_me("h/s", "sec", member))
ok("member: reads its notes and links", "classified" in b.read_note("decision/secret", viewer=member))
ok("member: reads public projects freely", "public zebra" in b.read_note("decision/open", viewer=member)
   and "Open zebra plan" in b.read_project("pub", viewer=member))
ok("member: another private project stays hidden", "other" not in b.read_projects(member))

# Tokens
tok = b.token("h/s")
ok("a token names its agent", b.agent_of_token(tok) == "h/s")
ok("a forged token is refused", b.agent_of_token("h/s:" + "0" * 40) is None and b.agent_of_token("") is None and b.agent_of_token(None) is None
   and b.agent_of_token("h/p:" + tok.split(":")[1]) is None)

# The writer
prompt, shown = b.writer_input("h/p", "pub", "slice")
ok("a public session's writer never sees private notes", "Secret" not in prompt and "Hidden" not in prompt and "decision/secret" not in shown)
prompt, shown = b.writer_input("h/s", "sec", "slice")
ok("a private session's writer sees its notes and public ones", "Secret" in prompt and "Open zebra" in prompt)
prompt, _ = b.writer_input("h/o", "other", "slice")
ok("... but not another private project's", "Secret" not in prompt)
got = b.apply({"notes": [
    {"id": "tech/both", "title": "Both zebra tool", "body": "LEAK-A"},
    {"id": "decision/open", "title": "Open zebra plan", "body": "LEAK-B"},
    {"id": "decision/fresh", "title": "Fresh", "body": "LEAK-C", "projects": ["pub"]},
    {"id": "requirement/newrule", "title": "New rule", "body": "LEAK-D", "scope": "global"}],
    "session": {"title": "s", "summary": "LEAK-E"}}, "src", "sec", shown)
v = b.Viewer("pub")
s = b.read_search("LEAK", viewer=v) if False else ""
for word in "ABCDE":
    ok(f"a private session can't put text where others read it ({word})", b.read_search(f"LEAK-{word}", viewer=v) == "No matches.\n", b.read_search(f"LEAK-{word}", viewer=v))
ok("the shared note it tried to change is untouched", b.note("tech/both")["body"] == "shared zebra" and b.note("decision/open")["body"] == "public zebra")
ok("what it wrote is in its own project", any("LEAK-A" in (b.note(i) or {}).get("body", "") for i in got) and all(b.note(i)["projects"] == ["sec"] for i in got), got)
ok("no new rule came from it", "New rule" not in b.read_rules())
_, shown = b.writer_input("h/p", "pub", "slice")
b.apply({"notes": [{"id": "tech/hidden", "title": "Hidden zebra tool", "body": "public text"},
                   {"id": "decision/x", "title": "X", "projects": ["sec", "pub"]}]}, "src", "pub", shown)
ok("a public session can't reach a private note by guessing its id", b.note("tech/hidden")["projects"] == ["sec"] and b.note("tech/hidden")["body"] == "hidden zebra")
ok("... or file into a private project", b.note("decision/x")["projects"] == ["pub"])

# Switching
ok("making it private reports notes shared with others", b.set_private("sec", True) >= 1)
b.set_private("sec", False)
ok("public again: everyone reads it", "classified" in b.read_note("decision/secret", viewer=b.Viewer()))
b.set_private("sec", True)
b.rename("sec", "sec2")
ok("rename keeps it private", b.project("sec2")["private"] == 1 and "sec2" not in b.read_projects(b.Viewer()))
b.set_home("h/s", "pub")
ok("an agent moved out loses access at once", "decision/secret" not in b.read_search("zebra", viewer=b.Viewer(b.home_of("h/s"))))
