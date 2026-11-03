<!-- fleet:brain -->
## Fleet brain

Fleet keeps a brain shared by all of the user's projects: decisions (and what they ruled out),
requirements, rules for every agent, tech, components, problems and their fixes, conventions,
todos and milestones, all linked. Fleet writes it from agents' sessions; you only read it.

At the start of every task, read what concerns you: the rules for every agent, and your project's
overview, open todos and current decisions:

    curl -s --unix-socket ~/.local/state/fleet/brain.sock "http://fleet/me/$FLEET_AGENT"

(If `$FLEET_AGENT` is empty, Fleet didn't start you; read
`curl -sG --unix-socket ~/.local/state/fleet/brain.sock http://fleet/here --data-urlencode host="$(hostname)" --data-urlencode folder="$PWD" --data-urlencode home="$HOME"` instead.)

Before you decide or change something, search everything. Other projects' decisions apply when you
touch what they own (shared machines, services, infrastructure):

    curl -sG --unix-socket ~/.local/state/fleet/brain.sock http://fleet/search --data-urlencode q="WORDS"

Add `--data-urlencode project=NAME` to search one project. One note and what's linked to it:
`http://fleet/n/KIND/SLUG?depth=1`. How two notes connect: `http://fleet/path?from=ID&to=ID`.
Every project: `http://fleet/projects`.

Rules for every agent are binding. If what you're about to do contradicts a decision or a
requirement, tell the user before doing it.
<!-- /fleet:brain -->
