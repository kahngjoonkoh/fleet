---
name: fleet-pull
description: Resume work from the Fleet brain. Reads the rules for every agent and this folder's project (overview, open todos, milestones, current decisions, recent sessions), checks them against git, then summarizes where things stand and what's left. Trigger with /fleet-pull or "resume from fleet".
---

# /fleet-pull

Rebuild working context from Fleet's brain for the project this session is in. The brain is one
database shared by every project, reached through `~/.local/state/fleet/brain.sock` (on the hub and
on every device the hub knows). Everything below only reads it.

## Steps

1. If `~/.local/state/fleet/brain.sock` doesn't exist (`test -S`), say Fleet's brain isn't reachable
   from this machine and stop rather than guessing.

2. Read the rules and this project:

   ```bash
   SOCK=~/.local/state/fleet/brain.sock
   if [ -n "$FLEET_AGENT" ]; then
     curl -s --unix-socket $SOCK -H "X-Fleet-Token: $FLEET_TOKEN" "http://fleet/me/$FLEET_AGENT"
   else
     curl -sG --unix-socket $SOCK http://fleet/here --data-urlencode host="$(hostname)" \
       --data-urlencode folder="${CLAUDE_PROJECT_DIR:-$PWD}" --data-urlencode home="$HOME"
   fi
   ```

   If it says no project holds this folder, tell the user, list the projects it shows, and suggest
   creating one on Fleet's Projects tab. Still go through the rules, and search for this folder's
   name in case notes about it were filed elsewhere: `curl -sG --unix-socket $SOCK -H "X-Fleet-Token: $FLEET_TOKEN" http://fleet/search --data-urlencode q="NAME"`.

3. Read what the summary needs, in one command: the two or three most recent sessions it lists, and
   any decision or todo that looks load-bearing for picking up work:

   ```bash
   for id in ID1 ID2 ID3; do curl -s --unix-socket $SOCK -H "X-Fleet-Token: $FLEET_TOKEN" "http://fleet/n/$id?depth=1"; echo; done
   ```

4. Cross-check against the code: in the project's git repo, `git log --oneline -10` and `git status`.
   Note commits or uncommitted changes the brain doesn't mention, and todos that look done already.

5. Summarize for the user:
   - **Current state**: where the project stands, per the brain and git.
   - **Recent decisions**: the current ones that matter for the next step, each with its why.
   - **What's left**: the open todos and planned milestones; mark any that git shows are done
     (Fleet marks them done on the next /fleet-push).
   - **Rules to keep in mind**: only those that bear on this work.

   Keep it short, and end by asking which item to pick up, or start on the top one if the user asked
   to continue.
