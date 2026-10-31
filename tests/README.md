# Fleet's end-to-end tests

These drive the Fleet running on this hub the way a person does: sign in (with throwaway admin
accounts), open pages, press buttons, open terminals, and start real agents on the hub and on one
other device. Run them on the hub after a change is deployed (`./install.sh --hub`):

    tests/run.sh               # every free test, about 3 minutes
    tests/run.sh agents sleep  # some of them
    tests/run.sh --paid        # also brain_paid: a real Claude conversation and brain update

| File | Covers |
|---|---|
| agents.py | creating agents (refusals too), typing into one, reopening it, exit codes, restart, stop, remove, who may do what |
| sleep.py | Settings → Agents, sleeping idle agents, waking on open, pinning, shells never sleeping |
| terminal.py | the Terminal tab: shell, resize, CSP, refusing other sites and signed-out use |
| speed_and_rename.py | cache headers, prefetch, returning to the page an action came from, renaming agents |
| projects.py | name-only projects with agents on two devices, moving agents between projects, AGENTS.md |
| project_field.py | the Project choice on the New agent form |
| brain_paid.py | one Claude conversation filed into the right projects, a rule for every agent, search, editing a note |

They need a second device in `fleet devices` (`FLEET_TEST_REMOTE`, default `e4`). Everything they make is
named `fleet-test-*` and removed by `cleanup.py` after each file, pass or fail; nothing else is touched.
brain_paid's writer may also add to shared notes that already exist (a `tech/redis` note, say), as a
real session would.

Don't stop `run.sh` partway: work it started in fleetd (an agent, a brain update) carries on and can
finish after cleanup. If you did, wait a few minutes and run the cleanup again:
`docker exec -i fleet-web python - < tests/cleanup.py`.
