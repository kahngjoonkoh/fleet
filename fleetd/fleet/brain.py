"""The brain: one database of small linked notes (decisions, requirements, tech, components, problems,
todos, milestones, ...) shared by every project, kept on the hub. Fleet writes it from agents'
conversations; agents only read it, through brain.sock, which fleetd serves on the hub and forwards
to every device over ssh at the same path.

Each note says which projects it concerns (any number, or none) and its scope: "project", or
"global" for rules every agent follows. Any agent can search everything; what it reads at the
start of a task is the global rules and its own project's overview, todos and decisions.

  ~/.local/share/fleet/brain.sqlite3   projects, notes, links, search index, read positions
  ~/.local/share/fleet/brain/KIND/SLUG.md   every note again as markdown, for Obsidian
"""

import asyncio
import json
import os
import re
import shutil
import sqlite3
import time
from pathlib import Path

import aiohttp

DATA = Path(os.environ.get("FLEET_DATA_DIR", Path.home() / ".local/share/fleet"))
DB_PATH = DATA / "brain.sqlite3"
MD_DIR = DATA / "brain"
PROJECT = re.compile(r"^[a-z0-9][a-z0-9-]{0,47}$")
NOTE_ID = re.compile(r"^([a-z][a-z0-9-]{0,31})/([a-z0-9][a-z0-9-]{0,95})$")

# Kinds the writer starts from; it may add others when none fits. Shared kinds are the same thing in
# every project (aiohttp is aiohttp everywhere), so a note of one of them joins every project it's used in.
KINDS = ["decision", "requirement", "tech", "component", "problem", "convention", "todo", "milestone",
         "question", "machine", "service", "concept", "person"]
SHARED = {"tech", "machine", "service", "concept", "person"}
RELATIONS = ["uses", "depends_on", "part_of", "implements", "chose_over", "replaces", "supersedes", "fixes",
             "caused_by", "blocks", "runs_on", "decided_in", "achieved_by", "relates_to"]
CLOSED = ("done", "fixed", "superseded", "reached")
RULE_KINDS = {"requirement", "convention"}  # the only kinds that can be a rule for every agent

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (name TEXT PRIMARY KEY, created INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notes (id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '', status TEXT, scope TEXT NOT NULL DEFAULT 'project', date TEXT,
    created TEXT NOT NULL, updated TEXT NOT NULL, sources TEXT NOT NULL DEFAULT '[]');
CREATE TABLE IF NOT EXISTS links (src TEXT NOT NULL, rel TEXT NOT NULL, dst TEXT NOT NULL, PRIMARY KEY (src, rel, dst));
CREATE INDEX IF NOT EXISTS links_dst ON links (dst);
CREATE TABLE IF NOT EXISTS note_projects (note TEXT NOT NULL, project TEXT NOT NULL, PRIMARY KEY (note, project));
CREATE INDEX IF NOT EXISTS note_projects_project ON note_projects (project);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5 (id UNINDEXED, title, body, tokenize = 'porter unicode61');
-- how far each agent's conversation has been read ("device/name"), and the project each agent is in
CREATE TABLE IF NOT EXISTS digests (agent TEXT PRIMARY KEY, offset INTEGER NOT NULL DEFAULT 0, last INTEGER);
CREATE TABLE IF NOT EXISTS homes (agent TEXT PRIMARY KEY, project TEXT NOT NULL);
-- who writes the brain (Settings > Brain): mode, model, key, base_url
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


class Problem(Exception):
    """Something to tell the person who asked."""


_db = None


def db():
    global _db
    if _db is None:
        DATA.mkdir(parents=True, exist_ok=True)
        _db = sqlite3.connect(DB_PATH, isolation_level=None)
        _db.row_factory = sqlite3.Row
        _db.execute("PRAGMA journal_mode = WAL")
        cols = [r[1] for r in _db.execute("PRAGMA table_info(projects)")]
        if "folder" in cols:  # projects used to have one device and folder; now agents anywhere join them
            _db.executescript("ALTER TABLE projects RENAME TO projects_old;"
                              "CREATE TABLE projects (name TEXT PRIMARY KEY, created INTEGER NOT NULL);"
                              "INSERT INTO projects SELECT name, created FROM projects_old; DROP TABLE projects_old;")
        _db.executescript(SCHEMA)
    return _db


def slugify(text, n=80):
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")[:n].strip("-") or "note"


def today():
    return time.strftime("%Y-%m-%d")


# --- projects ---------------------------------------------------------------------------

def projects():
    return [dict(r) for r in db().execute("SELECT * FROM projects ORDER BY name")]


def project(name):
    row = db().execute("SELECT * FROM projects WHERE name = ?", (name or "",)).fetchone()
    if row is None:
        raise Problem(f"There's no project called {name}.")
    return dict(row)


def create(name):
    if not PROJECT.match(name):
        raise Problem("Project names are 1-48 lowercase letters, digits or dashes.")
    try:
        db().execute("INSERT INTO projects VALUES (?, ?)", (name, int(time.time())))
    except sqlite3.IntegrityError:
        raise Problem(f"There's already a project called {name}.")
    upsert(f"overview/{name}", "overview", name, projects=[name],
           body=f"{name}: Fleet fills this in from its agents' work.")
    return project(name)


def delete(name):
    project(name)
    d = db()
    d.execute("DELETE FROM projects WHERE name = ?", (name,))
    d.execute("DELETE FROM homes WHERE project = ?", (name,))
    d.execute("DELETE FROM note_projects WHERE project = ?", (name,))
    # Notes that concerned only this project go with it. Shared things (tech, machines, ...) and rules
    # for every agent stay, unless nothing links to them any more once the rest is gone.
    orphans = lambda: [r[0] for r in d.execute(
        "SELECT id FROM notes WHERE scope != 'global' AND id NOT IN (SELECT note FROM note_projects)")]
    for note_id in orphans():
        if note_id.split("/")[0] not in SHARED:
            delete_note(note_id)
    for note_id in orphans():
        if not d.execute("SELECT 1 FROM links WHERE dst = ? OR src = ? LIMIT 1", (note_id, note_id)).fetchone():
            delete_note(note_id)


def rename(old, new):
    """Renames a project: its notes, its agents and its overview note (overview/OLD becomes
    overview/NEW, links included) all move to the new name in one transaction."""
    project(old)
    if not PROJECT.match(new):
        raise Problem("Project names are 1-48 lowercase letters, digits or dashes.")
    if new == old:
        return project(old)
    d = db()
    if d.execute("SELECT 1 FROM projects WHERE name = ?", (new,)).fetchone():
        raise Problem(f"There's already a project called {new}.")
    was, now = f"overview/{old}", f"overview/{new}"
    d.execute("BEGIN")
    try:
        d.execute("UPDATE projects SET name = ? WHERE name = ?", (new, old))
        d.execute("UPDATE note_projects SET project = ? WHERE project = ?", (new, old))
        d.execute("UPDATE homes SET project = ? WHERE project = ?", (new, old))
        if not d.execute("SELECT 1 FROM notes WHERE id = ?", (now,)).fetchone():
            d.execute("UPDATE notes SET id = ?, title = CASE WHEN title = ? THEN ? ELSE title END WHERE id = ?",
                      (now, old, new, was))
            d.execute("UPDATE OR IGNORE note_projects SET note = ? WHERE note = ?", (now, was))
            d.execute("UPDATE OR IGNORE links SET src = ? WHERE src = ?", (now, was))
            d.execute("UPDATE OR IGNORE links SET dst = ? WHERE dst = ?", (now, was))
            for table, col in (("note_projects", "note"), ("notes_fts", "id")):
                d.execute(f"DELETE FROM {table} WHERE {col} = ?", (was,))
            d.execute("DELETE FROM links WHERE src = ? OR dst = ?", (was, was))
            row = d.execute("SELECT title, body FROM notes WHERE id = ?", (now,)).fetchone()
            if row:
                d.execute("INSERT INTO notes_fts (id, title, body) VALUES (?, ?, ?)", (now, row["title"], row["body"]))
        d.execute("COMMIT")
    except BaseException:
        d.execute("ROLLBACK")
        raise
    # The markdown copies list each note's projects, so rewrite this project's.
    (MD_DIR / "overview" / f"{old}.md").unlink(missing_ok=True)
    for (note_id,) in d.execute("SELECT note FROM note_projects WHERE project = ?", (new,)).fetchall():
        export(note_id)
    return project(new)


def instructions_block():
    """What goes in the AGENTS.md of every folder a project's agent works in: that the brain exists,
    and how to read it. It doesn't name a project; each agent's /me says which one it's in."""
    sock = "~/.local/state/fleet/brain.sock"
    return f"""## Fleet brain

Fleet keeps a brain shared by all of the user's projects: decisions (and what they ruled out),
requirements, rules for every agent, tech, components, problems and their fixes, conventions,
todos and milestones, all linked. Fleet writes it from agents' sessions; you only read it.

At the start of every task, read what concerns you: the rules for every agent, and your project's
overview, open todos and current decisions:

    curl -s --unix-socket {sock} "http://fleet/me/$FLEET_AGENT"

(If `$FLEET_AGENT` is empty, Fleet didn't start you; read
`curl -sG --unix-socket {sock} http://fleet/here --data-urlencode host="$(hostname)" --data-urlencode folder="$PWD" --data-urlencode home="$HOME"` instead.)

Before you decide or change something, search everything. Other projects' decisions apply when you
touch what they own (shared machines, services, infrastructure):

    curl -sG --unix-socket {sock} http://fleet/search --data-urlencode q="WORDS"

Add `--data-urlencode project=NAME` to search one project. One note and what's linked to it:
`http://fleet/n/KIND/SLUG?depth=1`. How two notes connect: `http://fleet/path?from=ID&to=ID`.
Every project: `http://fleet/projects`.

Rules for every agent are binding. If what you're about to do contradicts a decision or a
requirement, tell the user before doing it."""


# --- notes ------------------------------------------------------------------------------

def note(note_id):
    row = db().execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    if row is None:
        return None
    n = dict(row)
    n["sources"] = json.loads(n["sources"])
    n["projects"] = [r[0] for r in db().execute("SELECT project FROM note_projects WHERE note = ? ORDER BY project", (note_id,))]
    n["links"] = {}
    for r in db().execute("SELECT rel, dst FROM links WHERE src = ? ORDER BY rel, dst", (note_id,)):
        n["links"].setdefault(r["rel"], []).append(r["dst"])
    return n


def notes(project=None, kind=None, scope=None):
    """Notes, without bodies, as {id: note}; each with its projects."""
    where, args = ["1"], []
    if project:
        where.append("id IN (SELECT note FROM note_projects WHERE project = ?)")
        args.append(project)
    if kind:
        where.append("kind = ?")
        args.append(kind)
    if scope:
        where.append("scope = ?")
        args.append(scope)
    rows = db().execute(f"SELECT id, kind, title, status, scope, date, created, updated FROM notes WHERE {' AND '.join(where)} "
                        "ORDER BY kind, title COLLATE NOCASE", args)
    out = {r["id"]: dict(r) | {"projects": []} for r in rows}
    if out:
        for r in db().execute("SELECT note, project FROM note_projects"):
            if r["note"] in out:
                out[r["note"]]["projects"].append(r["project"])
    return out


def titles(ids):
    ids = list(ids)
    if not ids:
        return {}
    q = ",".join("?" * len(ids))
    return {r[0]: r[1] for r in db().execute(f"SELECT id, title FROM notes WHERE id IN ({q})", ids)}


def upsert(note_id, kind, title, *, body=None, status=None, scope=None, date=None, projects=(), links=(), source=None):
    """Adds a note or updates one: given fields replace the old ones, projects and links accumulate."""
    d, now = db(), today()
    old = d.execute("SELECT sources FROM notes WHERE id = ?", (note_id,)).fetchone()
    if old is None:
        d.execute("INSERT INTO notes (id, kind, title, body, status, scope, date, created, updated, sources) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (note_id, kind, title[:200], body or "", status, scope or "project", date, now, now,
                   json.dumps([source] if source else [])))
    else:
        sources = json.loads(old["sources"])
        if source and source not in sources:
            sources = (sources + [source])[-20:]
        d.execute("UPDATE notes SET kind = ?, title = COALESCE(?, title), body = COALESCE(?, body), "
                  "status = COALESCE(?, status), scope = COALESCE(?, scope), date = COALESCE(?, date), "
                  "updated = ?, sources = ? WHERE id = ?",
                  (kind, title[:200] if title else None, body, status, scope, date, now, json.dumps(sources), note_id))
    for p in projects:
        d.execute("INSERT OR IGNORE INTO note_projects VALUES (?, ?)", (note_id, p))
    for rel, dst in links:
        if dst != note_id:
            d.execute("INSERT OR IGNORE INTO links VALUES (?, ?, ?)", (note_id, rel, dst))
    row = d.execute("SELECT title, body FROM notes WHERE id = ?", (note_id,)).fetchone()
    d.execute("DELETE FROM notes_fts WHERE id = ?", (note_id,))
    d.execute("INSERT INTO notes_fts (id, title, body) VALUES (?, ?, ?)", (note_id, row["title"], row["body"]))


def edit(note_id, title, body, status, scope, projects_):
    """A person's edit from the note page: replaces the fields and the project list outright."""
    if note(note_id) is None:
        raise Problem("There's no such note.")
    known = {p["name"] for p in projects()}
    d = db()
    d.execute("UPDATE notes SET title = ?, body = ?, status = ?, scope = ?, updated = ? WHERE id = ?",
              (title.strip()[:200] or note_id, body.strip(), status.strip() or None, scope, today(), note_id))
    d.execute("DELETE FROM note_projects WHERE note = ?", (note_id,))
    for p in projects_:
        if p in known:
            d.execute("INSERT INTO note_projects VALUES (?, ?)", (note_id, p))
    d.execute("DELETE FROM notes_fts WHERE id = ?", (note_id,))
    d.execute("INSERT INTO notes_fts (id, title, body) VALUES (?, ?, ?)", (note_id, title, body))
    export(note_id)


def add_rule(title, body):
    note_id = new_id("requirement", slugify(title))
    upsert(note_id, "requirement", title.strip(), body=body.strip(), scope="global", status="active", source="added by hand")
    export(note_id)
    return note_id


def new_id(kind, slug):
    """kind/slug, or kind/slug-2, -3, ... when that's taken."""
    note_id, n = f"{kind}/{slug}", 2
    while db().execute("SELECT 1 FROM notes WHERE id = ?", (note_id,)).fetchone():
        note_id, n = f"{kind}/{slug[:90]}-{n}", n + 1
    return note_id


def delete_note(note_id):
    d = db()
    for table, col in (("notes", "id"), ("notes_fts", "id"), ("note_projects", "note")):
        d.execute(f"DELETE FROM {table} WHERE {col} = ?", (note_id,))
    d.execute("DELETE FROM links WHERE src = ? OR dst = ?", (note_id, note_id))
    kind, _, slug = note_id.partition("/")
    (MD_DIR / kind / f"{slug}.md").unlink(missing_ok=True)


def export(note_id):
    """The note as markdown for Obsidian: properties, body, then its links as [[wikilinks]]."""
    n = note(note_id)
    if n is None or not NOTE_ID.match(note_id):
        return
    kind, slug = note_id.split("/", 1)
    props = [f"kind: {kind}", f"title: {json.dumps(n['title'], ensure_ascii=False)}", f"scope: {n['scope']}"]
    if n["projects"]:
        props.append("projects: [" + ", ".join(n["projects"]) + "]")
    for key in ("status", "date"):
        if n.get(key):
            props.append(f"{key}: {json.dumps(n[key], ensure_ascii=False)}")
    names = titles(t for ts in n["links"].values() for t in ts)
    links = "".join(f"- {rel.replace('_', ' ')}: " + ", ".join(f"[[{t}|{names.get(t, t)}]]" for t in ts) + "\n"
                    for rel, ts in n["links"].items())
    path = MD_DIR / kind / f"{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("---\n" + "\n".join(props) + "\n---\n" + f"# {n['title']}\n\n" +
                   (n["body"].strip() + "\n" if n["body"].strip() else "") + (f"\n## Links\n{links}" if links else ""))
    os.replace(tmp, path)


def backlinks(note_id):
    return [(r["src"], r["rel"]) for r in db().execute("SELECT src, rel FROM links WHERE dst = ? ORDER BY rel", (note_id,))]


# --- agents' home projects --------------------------------------------------------------

def home_of(agent):
    """The project an agent ("device/name") is in, or None."""
    row = db().execute("SELECT project FROM homes WHERE agent = ?", (agent,)).fetchone()
    return row[0] if row else None


def set_home(agent, name):
    """Puts an agent in a project, or (name "") in none."""
    if not name:
        db().execute("DELETE FROM homes WHERE agent = ?", (agent,))
        return
    project(name)
    db().execute("INSERT INTO homes VALUES (?, ?) ON CONFLICT(agent) DO UPDATE SET project = excluded.project", (agent, name))


def rename_agent(old, new):
    """Keeps an agent's project and how far its conversation has been read when it's renamed."""
    db().execute("UPDATE homes SET agent = ? WHERE agent = ?", (new, old))
    db().execute("UPDATE digests SET agent = ? WHERE agent = ?", (new, old))


def forget_agent(agent):
    """A removed agent leaves no project membership or read position behind for a later one of its name."""
    db().execute("DELETE FROM homes WHERE agent = ?", (agent,))
    db().execute("DELETE FROM digests WHERE agent = ?", (agent,))


def members(name):
    return [r[0] for r in db().execute("SELECT agent FROM homes WHERE project = ? ORDER BY agent", (name,))]


WRITER_KEYS = ("mode", "model", "key", "base_url")


def writer():
    """{"mode": "subscription" | "anthropic" | "openai", "model", "key", "base_url"}"""
    stored = {r[0]: r[1] for r in db().execute("SELECT key, value FROM settings WHERE key LIKE 'writer_%'")}
    w = {k: stored.get(f"writer_{k}", "") for k in WRITER_KEYS}
    w["mode"] = w["mode"] or "subscription"
    w["model"] = w["model"] or "haiku"
    return w


def set_writer(values):
    for k in WRITER_KEYS:
        if k in values:
            db().execute("INSERT INTO settings VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (f"writer_{k}", str(values[k])))


def read_offset(agent):
    row = db().execute("SELECT offset FROM digests WHERE agent = ?", (agent,)).fetchone()
    return row[0] if row else 0


def save_offset(agent, offset):
    db().execute("INSERT INTO digests VALUES (?, ?, ?) ON CONFLICT(agent) DO UPDATE SET offset = excluded.offset, "
                 "last = excluded.last", (agent, offset, int(time.time())))


# --- search and graph -------------------------------------------------------------------

def search(q, project=None, limit=25):
    words = [w for w in re.findall(r"\w+", (q or "").lower()) if len(w) > 1][:8]
    if not words:
        raise Problem("Search for some words, e.g. q=sleep tmux")
    match = " ".join(f'"{w}"*' for w in words)
    sql = ("SELECT n.id, n.kind, n.title, n.status, n.scope, snippet(notes_fts, 2, '', '', ' … ', 24) AS snip "
           "FROM notes_fts JOIN notes n ON n.id = notes_fts.id WHERE notes_fts MATCH ?")
    args = [match]
    if project:
        sql += " AND (n.id IN (SELECT note FROM note_projects WHERE project = ?) OR n.scope = 'global')"
        args.append(project)
    rows = [dict(r) for r in db().execute(sql + " ORDER BY bm25(notes_fts, 0, 5, 1) LIMIT ?", args + [limit])]
    labels = {}
    for r in db().execute("SELECT note, project FROM note_projects"):
        labels.setdefault(r[0], []).append(r[1])
    for r in rows:
        r["projects"] = labels.get(r["id"], [])
    return rows


def _neighbour_ids(note_id):
    return {r[0] for r in db().execute("SELECT dst FROM links WHERE src = ? UNION SELECT src FROM links WHERE dst = ?",
                                       (note_id, note_id))}


def neighbourhood(note_id, depth=1):
    seen, frontier = {note_id}, {note_id}
    for _ in range(max(0, min(depth, 3))):
        frontier = {m for n in frontier for m in _neighbour_ids(n)} - seen
        seen |= frontier
    return seen


def path(a, b, limit=6):
    """The shortest chain of links between two notes, either direction: [(id, rel, id), ...] or None.
    Session notes link to everything they touched, so a chain never passes through one."""
    if a == b:
        return []
    prev, frontier = {a: None}, [a]
    for _ in range(limit):
        nxt = []
        for n in frontier:
            for r in db().execute("SELECT src, rel, dst FROM links WHERE src = ? OR dst = ?", (n, n)):
                other = r["dst"] if r["src"] == n else r["src"]
                if other not in prev and (other == b or not other.startswith("session/")):
                    prev[other] = (n, r["src"], r["rel"], r["dst"])
                    if other == b:
                        steps, cur = [], b
                        while prev[cur]:
                            _, s, rel, dst = prev[cur]
                            steps.append((s, rel, dst))
                            cur = prev[cur][0]
                        return steps[::-1]
                    nxt.append(other)
        frontier = nxt
    return None


def _edges(ids):
    if not ids:
        return []
    q = ",".join("?" * len(ids))
    return [{"source": r[0], "target": r[2], "rel": r[1]} for r in
            db().execute(f"SELECT src, rel, dst FROM links WHERE src IN ({q}) AND dst IN ({q})", list(ids) * 2)]


def graph(project_name):
    """The project's notes, plus the notes outside it they link to (marked outside)."""
    own = {i: n for i, n in notes(project_name).items() if n["kind"] != "overview"}
    outside = {m for i in own for m in _neighbour_ids(i)} - set(own)
    extra = notes()
    nodes = [{"id": i, "kind": n["kind"], "title": n["title"], "status": n["status"]} for i, n in own.items()]
    nodes += [{"id": i, "kind": extra[i]["kind"], "title": extra[i]["title"], "status": extra[i]["status"], "outside": True}
              for i in outside if i in extra]
    return {"nodes": nodes, "edges": _edges({n["id"] for n in nodes})}


def graph_all():
    """Projects, the shared things and notes that join them, and the rules for every agent."""
    all_notes, names = notes(), [p["name"] for p in projects()]
    nodes = [{"id": f"project/{p}", "kind": "project", "title": p} for p in names]
    edges = []
    for i, n in all_notes.items():
        if n["kind"] == "overview":
            continue
        if n["scope"] == "global" or (n["kind"] in SHARED and n["projects"]) or len(n["projects"]) > 1:
            nodes.append({"id": i, "kind": n["kind"], "title": n["title"], "status": n["status"]})
            edges += [{"source": f"project/{p}", "target": i, "rel": "concerns"} for p in n["projects"]]
    return {"nodes": nodes, "edges": edges}


def summary(name):
    rows = notes(name)
    count = lambda kind, open_=False: sum(1 for n in rows.values() if n["kind"] == kind and (not open_ or n["status"] not in CLOSED))
    updated = db().execute("SELECT max(updated) FROM notes WHERE id IN (SELECT note FROM note_projects WHERE project = ?) "
                           "AND kind != 'overview'", (name,)).fetchone()[0]
    return {"notes": sum(1 for n in rows.values() if n["kind"] != "overview"), "todos_open": count("todo", True),
            "milestones": count("milestone"), "decisions": count("decision"), "updated": updated}


# --- reading, for agents (brain.sock) ----------------------------------------------------

def _line(note_id, n, show_projects=False):
    status = f" [{n['status']}]" if n.get("status") else ""
    where = f" ({', '.join(n['projects'])})" if show_projects and n.get("projects") else ""
    return f"- {note_id}: {n['title']}{status}{where}"


def read_rules():
    rows = notes(scope="global")
    if not rows:
        return "No rules for every agent yet.\n"
    out = ["# Rules for every agent", ""]
    for i in rows:
        n = note(i)
        out.append(f"- {n['title']}" + (f": {re.sub(r'\s+', ' ', n['body']).strip()}" if n["body"].strip() else "") + f" ({i})")
    return "\n".join(out) + "\n"


def read_project(name, agent=None):
    project(name)
    rows = notes(name)
    who = ", ".join(members(name)) or "none yet"
    out = [f"# Project {name}", (f"You are agent {agent}. " if agent else "") + f"Its agents: {who}.", "",
           (note(f"overview/{name}") or {}).get("body", ""), ""]
    for kind, title, keep in (("todo", "Open todos", lambda n: n["status"] not in CLOSED),
                              ("milestone", "Milestones", lambda n: True),
                              ("requirement", "Requirements", lambda n: n["scope"] != "global"),
                              ("decision", "Current decisions", lambda n: n["status"] != "superseded")):
        lines = [_line(i, n) for i, n in rows.items() if n["kind"] == kind and keep(n)]
        if lines:
            out += [f"## {title}", *lines, ""]
    others = sorted({n["kind"] for n in rows.values()} - {"overview", "todo", "milestone", "requirement", "decision", "session"})
    if others:
        out.append("## Other notes in this project")
        for kind in others:
            out += [_line(i, n) for i, n in rows.items() if n["kind"] == kind]
        out.append("")
    sessions = sorted((i for i, n in rows.items() if n["kind"] == "session"), reverse=True)[:5]
    if sessions:
        out += ["## Recent sessions", *[_line(i, rows[i]) for i in sessions], ""]
    out.append("Read a note with http://fleet/n/ID?depth=1; search with http://fleet/search?q=WORDS.")
    return "\n".join(out).strip() + "\n"


def read_me(agent, home):
    """What an agent reads at the start of a task: the rules, then its home project."""
    out = [read_rules()]
    if home:
        out.append(read_project(home, agent))
    else:
        out.append(f"You are agent {agent}, which isn't part of a project. Projects:\n" + read_projects())
    return "\n".join(out)


def read_projects():
    lines = []
    for p in projects():
        overview = re.sub(r"\s+", " ", (note(f"overview/{p['name']}") or {}).get("body", "")).strip()
        lines.append(f"- {p['name']}: {overview[:200]}")
    return "\n".join(lines) + "\n" if lines else "No projects yet.\n"


def read_note(note_id, depth=1):
    n = note(note_id)
    if n is None:
        raise Problem(f"No note {note_id}. Search with http://fleet/search?q=WORDS.")
    where = ", ".join(n["projects"]) or "no project"
    out = [f"# {n['title']}", f"{note_id} · {'rule for every agent' if n['scope'] == 'global' else where}"
           + (f" · {n['status']}" if n["status"] else "") + f" · updated {n['updated']}", "", n["body"] or "(no details yet)", ""]
    names = titles(neighbourhood(note_id, 1))
    links = [f"- {rel.replace('_', ' ')} {t}: {names.get(t, t)}" for rel, ts in n["links"].items() for t in ts]
    back = [f"- {src} ({rel.replace('_', ' ')} this): {names.get(src, src)}" for src, rel in backlinks(note_id)]
    if links:
        out += ["## Links", *links, ""]
    if back:
        out += ["## Linked from", *back, ""]
    if depth > 1:
        further = notes()
        ring = sorted(neighbourhood(note_id, depth) - neighbourhood(note_id, 1))
        if ring:
            out += [f"## Within {depth} links", *[_line(i, further[i], True) for i in ring if i in further]]
    return "\n".join(out).strip() + "\n"


def read_kind(name, kind):
    rows = notes(name, kind=kind)
    return "\n".join(_line(i, n) for i, n in rows.items()) + "\n" if rows else f"No {kind} notes in {name}.\n"


def read_search(q, project_name=None):
    rows = search(q, project_name)
    if not rows:
        return "No matches.\n"
    out = []
    for r in rows:
        where = "rule for every agent" if r["scope"] == "global" else ", ".join(r["projects"]) or "no project"
        out.append(f"- {r['id']}: {r['title']}" + (f" [{r['status']}]" if r["status"] else "") + f" ({where})"
                   + (f"\n  {re.sub(chr(10), ' ', r['snip'])}" if r["snip"] else ""))
    return "\n".join(out) + "\n"


def read_path(a, b):
    steps = path(a, b)
    if steps is None:
        return f"No chain of links from {a} to {b} within 6 steps.\n"
    names = titles({a, b} | {s for st in steps for s in (st[0], st[2])})
    return "\n".join(f"- {names.get(s, s)} --{rel.replace('_', ' ')}--> {names.get(d, d)}" for s, rel, d in steps) + "\n"


# --- the writer --------------------------------------------------------------------------

WRITER_PROMPT = f"""You keep the brain of a person's software projects: one database of small linked
notes shared by all their projects. You get the projects, the rules every agent follows, the notes
that may need updating, and a slice of one agent's work session. Record what the session added or
changed, and nothing else. Answer with the JSON object only.

Notes
- One note per thing, never one note per session. Kinds: {", ".join(KINDS)}. Make up another kind
  only when none fits.
- An id is "kind/slug" (lowercase, dashes). If the thing already has a note in the list, reuse that
  id exactly; never create a near-duplicate under another name.
- Link densely. Every note links to at least one other note. Give every tech, component, machine,
  service and concept you mention its own note (a one-line body is fine) so it can be linked.
  Relations: {", ".join(RELATIONS)}.
- projects: the projects (by name, from the list) the note concerns: the agent's home project for its
  own work, another project when the work was about that one (an infrastructure agent fixing a
  service another project uses), several when it concerns several, none for general facts.
- scope: "global" only on a requirement or convention that the user wants every agent everywhere
  to follow ("never ...", "always ...", "in all projects ..."); otherwise "project". Notes it links
  to (the tech or concept it's about) stay "project".
- decision: what was decided, why, and what was rejected (link chose_over); status active or
  superseded (link supersedes). requirement: something the user asked for or ruled out.
  problem: symptom, cause, fix, lesson; status open or fixed. convention: how things are done.
  todo: a concrete next step left undone; status open or done (mark listed todos done when the
  session finished them). milestone: a result reached or planned; status reached or planned, with date.
  question: something undecided.
- To update a note, reuse its id; include a body only when it should change, and then the whole new body.
- Bodies: short markdown, facts not narration. Never copy secrets, tokens, passwords or keys.
- overview: the home project's 3-6 sentence description, only if it should change.
- session: a title of at most 8 words and a summary of at most 3 sentences.
- If the slice holds nothing worth keeping, return empty notes and a session that says so briefly."""

WRITER_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "kind": {"type": "string"}, "title": {"type": "string"},
            "status": {"type": "string"}, "date": {"type": "string"}, "body": {"type": "string"},
            "projects": {"type": "array", "items": {"type": "string"}},
            "scope": {"type": "string", "enum": ["project", "global"]},
            "links": {"type": "array", "items": {"type": "object", "properties": {
                "rel": {"type": "string"}, "to": {"type": "string"}}, "required": ["rel", "to"]}}},
            "required": ["id", "kind", "title"]}},
        "overview": {"type": "string"},
        "session": {"type": "object", "properties": {"title": {"type": "string"}, "summary": {"type": "string"}},
                    "required": ["title", "summary"]},
    },
    "required": ["notes", "session"],
}

LISTED = 400  # notes from other projects the writer is shown, beyond the home project and shared things


def writer_input(agent, home, text):
    """The writer's prompt, and the ids it was shown (only those may be updated in place)."""
    everything = notes()
    shown, lines = [], []

    def add(i, n):
        if i not in shown:
            shown.append(i)
            lines.append(f"{i} | {n['title']}" + (f" | {n['status']}" if n["status"] else "")
                         + (f" | {', '.join(n['projects'])}" if n["projects"] else "") + (" | GLOBAL" if n["scope"] == "global" else ""))

    for i, n in everything.items():
        if n["scope"] == "global" or (home and home in n["projects"] and n["kind"] not in ("overview", "session")):
            add(i, n)
    for i, n in everything.items():
        if n["kind"] in SHARED:
            add(i, n)
    others = [(i, n) for i, n in everything.items() if n["kind"] in ("decision", "requirement", "todo", "component", "problem")
              and n["status"] not in CLOSED]
    for i, n in others[:LISTED]:
        add(i, n)
    overview = (note(f"overview/{home}") or {}).get("body", "") if home else ""
    prompt = (f"Projects (name: overview):\n{read_projects()}\n"
              f"This agent: {agent}. Home project: {home or 'none'}.\n"
              + (f"Home project overview: {overview}\n" if overview else "")
              + f"\nNotes (id | title | status | projects):\n" + ("\n".join(lines) or "(none yet)")
              + f"\n\nSession slice:\n{text}")
    return prompt, set(shown)


def apply(ops, source, home, shown):
    """Folds a writer's result into the brain and returns the ids it wrote. A new note whose id is
    already taken by a note the writer wasn't shown gets its own id, unless it's a shared kind
    (tech, machines, ...), which is meant to be one note everywhere."""
    known = {p["name"] for p in projects()}
    touched, written = [], []
    renamed = {}

    def clean(raw, kind=None):
        raw = (raw or "").strip().lower()
        if "/" not in raw and kind:
            raw = f"{kind}/{raw}"
        k, _, s = raw.partition("/")
        note_id = f"{slugify(k, 32)}/{slugify(s, 96)}"
        return note_id if NOTE_ID.match(note_id) else None

    items = []
    for raw in ops.get("notes", []):
        note_id = clean(raw.get("id"), slugify(raw.get("kind") or "", 32))
        if not note_id or not raw.get("title"):
            continue
        kind = note_id.split("/")[0]
        if note_id not in shown and kind not in SHARED and note(note_id) is not None:
            renamed[note_id] = new_id(kind, note_id.split("/", 1)[1])
            note_id = renamed[note_id]
        items.append((note_id, kind, raw))

    for note_id, kind, raw in items:
        given = [p for p in raw.get("projects") or [] if p in known]
        mine = given or ([home] if home else [])
        links = []
        for link in raw.get("links") or []:
            target = clean(link.get("to"))
            if not target:
                continue
            target = renamed.get(target, target)
            rel = slugify(link.get("rel") or "relates-to", 32).replace("-", "_")
            links.append((rel, target))
            if note(target) is None:  # a stub, so the link has somewhere to land
                upsert(target, target.split("/")[0], target.split("/", 1)[1].replace("-", " "), projects=mine, source=source)
                touched.append(target)
        upsert(note_id, kind, raw["title"], body=raw.get("body"), status=raw.get("status") or None,
               scope="global" if raw.get("scope") == "global" and kind in RULE_KINDS else None,
               date=(raw.get("date") or "")[:10] or None,
               projects=mine, links=links, source=source)
        touched.append(note_id)
        written.append(note_id)

    if ops.get("overview") and home:
        upsert(f"overview/{home}", "overview", home, body=ops["overview"], projects=[home], source=source)
        touched.append(f"overview/{home}")
    session = ops.get("session")
    if session and session.get("title") and written:
        sid = new_id("session", f"{today()}-{slugify(session['title'], 60)}")
        concerned = sorted({p for i in written for p in (note(i) or {}).get("projects", [])} | ({home} if home else set()))
        upsert(sid, "session", session["title"], body=session.get("summary", ""), date=today(), projects=concerned,
               links=[("touched", i) for i in written], source=source)
        touched.append(sid)
    for note_id in dict.fromkeys(touched):
        export(note_id)
    return list(dict.fromkeys(touched))


def _claude():
    return shutil.which("claude") or str(Path.home() / ".local/bin/claude")


async def write(prompt, writer):
    """Runs the writer (see writer()) and returns its JSON. The first two go through the claude CLI on this machine: with the
    signed-in Claude subscription, or with an Anthropic API key; the third calls any OpenAI-compatible API."""
    mode, model = writer.get("mode", "subscription"), writer.get("model") or "haiku"
    if mode == "openai":
        url = writer.get("base_url", "").rstrip("/") + "/chat/completions"
        body = {"model": model, "response_format": {"type": "json_object"},
                "messages": [{"role": "system", "content": WRITER_PROMPT + "\n\nJSON shape: " + json.dumps(WRITER_SCHEMA)},
                             {"role": "user", "content": prompt}]}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=300)) as s:
            async with s.post(url, json=body, headers={"Authorization": f"Bearer {writer.get('key', '')}"}) as r:
                if r.status != 200:
                    raise Problem(f"the writer API answered {r.status}: {(await r.text())[:200]}")
                return json.loads((await r.json())["choices"][0]["message"]["content"])
    env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    if mode == "anthropic":
        env["ANTHROPIC_API_KEY"] = writer.get("key", "")
    workdir = DATA / "writer"  # empty, so no project's CLAUDE.md is loaded
    workdir.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        _claude(), "-p", "--model", model, "--setting-sources", "", "--strict-mcp-config", "--tools", "",
        "--no-session-persistence", "--output-format", "json", "--system-prompt", WRITER_PROMPT,
        "--json-schema", json.dumps(WRITER_SCHEMA), cwd=workdir, env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(prompt.encode()), 300)
    except TimeoutError:
        proc.kill()
        raise Problem("the writer took more than 5 minutes")
    try:
        result = json.loads(out)
    except ValueError:
        raise Problem(f"claude exited {proc.returncode}: {(err or out).decode(errors='replace')[-300:]}")
    if result.get("is_error"):
        raise Problem(f"claude: {str(result.get('result'))[:300]}")
    data = result.get("structured_output")
    if data is None:  # older CLIs put the JSON in the text result
        text = result.get("result", "")
        data = json.loads(text[text.find("{"):text.rfind("}") + 1])
    return data


_lock = asyncio.Lock()


def lock():
    """One writer run at a time: each reads the notes the previous one wrote."""
    return _lock
