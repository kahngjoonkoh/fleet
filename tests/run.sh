#!/bin/sh
# End-to-end tests against the Fleet running on this hub: they sign in with throwaway admin accounts,
# use the real pages, and start real agents here and on one other device. Run on the hub:
#
#   tests/run.sh                 every free test
#   tests/run.sh agents sleep    just these
#   tests/run.sh --paid          also brain_paid, which talks to Claude (uses some of the hub's plan)
#
# FLEET_TEST_REMOTE is the other device (default e4; it must be in `fleet devices` and reachable with
# `ssh NAME`). After each test, cleanup.py removes whatever it made, even if it failed halfway.
set -u
cd "$(dirname "$0")"
REMOTE=${FLEET_TEST_REMOTE:-e4}
HUB=$(hostname)
FREE="agents sleep terminal speed_and_rename projects project_field"
paid=""; tests=""
for a in "$@"; do [ "$a" = "--paid" ] && paid=brain_paid || tests="$tests $a"; done
[ -n "$tests" ] || tests="$FREE"
tests="$tests $paid"

# Folders the tests start agents in, on both machines; removed again at the end, with the Claude
# Code conversations agents had in them.
REMOTE_DIRS="fleet-test-a fleet-test-rn fleet-test-mf fleet-test-pf fleet-test-pf2"
HUB_DIRS="fleet-test-mf-hub"
dirs() { # create|remove
  if [ "$1" = create ]; then
    ssh "$REMOTE" "cd ~/.cache && mkdir -p $REMOTE_DIRS" && (cd ~/.cache && mkdir -p $HUB_DIRS)
  else
    ssh "$REMOTE" "cd ~/.cache && rm -rf $REMOTE_DIRS; cd ~/.claude/projects 2>/dev/null && rm -rf -- -home-*--cache-fleet-test-*"
    (cd ~/.cache && rm -rf $HUB_DIRS); (cd ~/.claude/projects 2>/dev/null && rm -rf -- -home-*--cache-fleet-test-*)
  fi
}
in_web() { docker exec -i -e FLEET_TEST_REMOTE="$REMOTE" -e FLEET_TEST_HUB="$HUB" fleet-web python - < "$1"; }

dirs create
trap 'dirs remove' EXIT
failed=0
for t in $tests; do
  echo "== $t"
  out=$(in_web "$t.py" 2>&1)
  echo "$out" | grep -E "^(PASS|FAIL)"
  echo "$out" | grep -qE "^FAIL|Traceback" && { failed=$((failed + 1)); echo "$out" | grep -vE "^(PASS|FAIL)" | tail -5; }
  in_web cleanup.py
done
echo "== $([ $failed = 0 ] && echo "all passed" || echo "$failed test file(s) failed")"
exit $failed
