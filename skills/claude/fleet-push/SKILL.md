---
name: fleet-push
description: Save the current session to the Fleet brain. Fleet reads this conversation and files its decisions, requirements, problems and fixes, todos, milestones and tech as linked notes under the projects they concern. Trigger with /fleet-push [project] or "save this session to fleet".
---

# /fleet-push

Ask Fleet to file this session into its brain now. Fleet writes the notes itself from the conversation
file (with Claude Haiku on the hub, or whatever Settings → Brain says); you don't write them. It reads
only what's new since the last save, so saving again later costs little.

The brain is reached through `~/.local/state/fleet/brain.sock`, which exists on the hub and on every
device the hub knows (`fleet devices`).

## Steps

1. If `~/.local/state/fleet/brain.sock` doesn't exist (`test -S`), say Fleet's brain isn't reachable
   from this machine and stop. Don't write notes anywhere else.

2. If `$FLEET_AGENT` is set, Fleet started this session as an agent; save it by name:

   ```bash
   curl -s --unix-socket ~/.local/state/fleet/brain.sock -H "X-Fleet-Token: $FLEET_TOKEN" http://fleet/save --data-urlencode agent="$FLEET_AGENT"
   ```

3. Otherwise save this session's file. It's the recently written `.jsonl` for this session's folder
   that already contains this /fleet-push request (other sessions may be writing files there too):

   ```bash
   dir="${CLAUDE_PROJECT_DIR:-$PWD}"
   d=~/.claude/projects/$(printf %s "$dir" | sed 's/[^a-zA-Z0-9]/-/g')
   f=$(ls -t "$d"/*.jsonl 2>/dev/null | head -5 | xargs -r grep -l "fleet-push" | head -1)
   [ -n "$f" ] || f=$(ls -t ~/.claude/projects/*/*.jsonl | head -10 | xargs -r grep -l "fleet-push" | head -1)
   echo "session file: $f"
   curl -s --unix-socket ~/.local/state/fleet/brain.sock http://fleet/save \
     --data-urlencode host="$(hostname)" --data-urlencode file="$f" \
     --data-urlencode folder="$dir" --data-urlencode home="$HOME"
   ```

   This can take a few minutes for a long session. If no file is found, say so and stop.

   If the user named a project (e.g. `/fleet-push devops`), add `--data-urlencode project=NAME` to
   the curl command in step 2 or 3, so the session is filed under that project whatever its folder.

4. Tell the user what Fleet saved: its list of notes, briefly (kind and title; group them if there
   are many). If Fleet says there's more of the session left to read, run the save again. If it
   says nothing new, say the session was already saved. If it returns an error, show it.

Don't commit anything, and don't edit notes yourself; corrections go through the note's page on
Fleet's Projects tab.
