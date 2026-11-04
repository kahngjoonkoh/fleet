---
description: Save this session to the Fleet brain
argument-hint: "[project]"
---
Save this session to the Fleet brain now. Fleet writes the notes itself from the session file. Don't write notes anywhere else, don't edit notes, and don't commit anything.

Run exactly this with bash, and allow it up to 10 minutes:

```bash
SOCK=~/.local/state/fleet/brain.sock
PROJECT='${1:-none}'
if [ ! -S "$SOCK" ]; then
  echo "Fleet's brain isn't reachable from this machine."
elif [ -n "$FLEET_AGENT" ]; then
  FORM=(--data-urlencode "agent=$FLEET_AGENT")
else
  dir=${PI_CODING_AGENT_SESSION_DIR:-$HOME/.pi/agent/sessions/--$(printf %s "$PWD" | sed 's|^/||; s|[/\\:]|-|g')--}
  f=$(ls -t "$dir"/*.jsonl 2>/dev/null | head -5 | xargs -r grep -l "Save this session to the Fleet brain" | head -1)
  [ -n "$f" ] || f=$(ls -t "$dir"/*.jsonl 2>/dev/null | head -1)
  if [ -n "$f" ]; then
    FORM=(--data-urlencode "host=$(hostname)" --data-urlencode "file=$f" --data-urlencode "folder=$PWD" --data-urlencode "home=$HOME")
  else
    echo "No session file found in $dir."
  fi
fi
if [ -n "${FORM[*]}" ]; then
  [ "$PROJECT" = none ] || FORM+=(--data-urlencode "project=$PROJECT")
  curl -s --max-time 590 --unix-socket "$SOCK" -H "X-Fleet-Token: $FLEET_TOKEN" http://fleet/save "${FORM[@]}"
fi
```

Then tell the user the result:
- If it lists notes, give each one's kind and title briefly, grouped if there are many.
- If it says there's more of the session left to read, run the same command again.
- If it says there's nothing new, say the session was already saved.
- If it says the brain isn't reachable, no session file was found, or returns an error, show that and stop.
