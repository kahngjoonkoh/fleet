---
description: Resume work from the Fleet brain
argument-hint: "[what to pick up]"
---
Rebuild working context from Fleet's brain for the project this session is in, then summarize it. Everything here only reads the brain. Don't change any files.

1. Read the rules and this project. Run this with bash:

```bash
SOCK=~/.local/state/fleet/brain.sock
if [ ! -S "$SOCK" ]; then
  echo "Fleet's brain isn't reachable from this machine."
elif [ -n "$FLEET_AGENT" ]; then
  curl -s --unix-socket "$SOCK" -H "X-Fleet-Token: $FLEET_TOKEN" "http://fleet/me/$FLEET_AGENT"
else
  curl -sG --unix-socket "$SOCK" http://fleet/here --data-urlencode host="$(hostname)" --data-urlencode folder="$PWD" --data-urlencode home="$HOME"
fi
```

If the brain isn't reachable, say so and stop; don't guess. If it says no project holds this folder, tell the user, list the projects it shows, and suggest creating one on Fleet's Projects tab. Still read the rules, and search for this folder's name in case notes about it were filed elsewhere:

```bash
curl -sG --unix-socket ~/.local/state/fleet/brain.sock -H "X-Fleet-Token: $FLEET_TOKEN" http://fleet/search --data-urlencode q="FOLDER_NAME"
```

2. Read the two or three most recent sessions it lists, and any decision or todo that matters for picking up work. Put their ids in place of ID1 ID2 ID3:

```bash
for id in ID1 ID2 ID3; do curl -s --unix-socket ~/.local/state/fleet/brain.sock -H "X-Fleet-Token: $FLEET_TOKEN" "http://fleet/n/$id?depth=1"; echo; done
```

3. Check against the code. In the project's git repo run `git log --oneline -10` and `git status`. Note commits or uncommitted changes the brain doesn't mention, and todos that look done already.

4. Summarize for the user, keeping it short:
- **Current state:** where the project stands, per the brain and git.
- **Recent decisions:** the current ones that matter for the next step, each with its why.
- **What's left:** open todos and planned milestones. Mark any that git shows are done.
- **Rules to keep in mind:** only those that bear on this work.

The user asked to pick up: ${@:-nothing specific}. If that names something, start on it after the summary. Otherwise, end by asking which item to pick up.
