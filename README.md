# Fleet

A dashboard for the Claude Code sessions running on your machines.

- **fleetd** runs on every host as a systemd user service. It does the host-side work (agents and the
  Terminal tab's shells) and listens only on a unix socket.
- **fleet-web** runs on one host, the hub, in a locked-down Docker container. It handles sign-in and
  accounts and talks to fleetd. There is no sign-up page; an admin creates every account.

## Install

Every machine needs git and curl; the hub also needs Docker. Nothing needs sudo, and the installer
fetches [uv](https://docs.astral.sh/uv/) if it's missing.

On the hub (once):

```sh
curl -fsSL https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/install.sh | sh -s -- --hub --origin https://fleet.example.com
fleet admin create you@example.com      # prints a one-time password
```

Point your tunnel (cloudflared or similar) for that origin at `http://fleet-web:8080` on the Docker
network `public_proxy` (change it with `FLEET_NETWORK` in `.env`). Fleet never publishes a host port.

On every other machine, then add it on the hub:

```sh
curl -fsSL https://raw.githubusercontent.com/kahngjoonkoh/fleet/main/install.sh | sh
fleet devices add NAME                  # on the hub; NAME is how the hub reaches it with ssh
```

Rerun the same curl line to update; the hub remembers its settings in `.env`. From a checkout,
`./install.sh [--hub ...]` installs that checkout instead.

To have fleetd start at boot rather than at your first login, run once:
`sudo loginctl enable-linger $USER`.

The Devices tab shows each machine's connection and versions. Add `--show-hardware` to the hub
install to also show CPU, memory, disk and GPUs.

## Agents

An admin starts an agent from the Agents tab: a name, a device, a folder, and what to run (Claude
Code, Codex, Pi, a shell or any command; only what's installed on that device is offered). Each agent
runs as its own systemd user unit, `fleet-agent@NAME`, holding its own tmux server, so it keeps
running when the browser, ssh, fleetd or the hub goes away, and one agent failing doesn't touch the
others. Open it from the tab to work in it; closing the page only detaches.

- An agent that exits stays on screen with its exit code until you restart or stop it.
- Start, and a reboot if "Start again after a reboot" is on, resume where it left off: Claude Code
  and Pi reopen the agent's own conversation.
- When memory runs out, the kernel stops an agent before fleetd or sshd.
- Without linger (above), agents stop when you log out of that machine.
- Its files are in `~/.local/state/fleet/agents/`. Without Fleet, `ssh` there and run
  `tmux -S ~/.local/state/fleet/agents/NAME.sock attach`.

Other tmux sessions on each machine are listed below the agents; Fleet can open them but leaves them alone.

Idle agents (no output and no typing for 30 minutes, set in Settings → Agents) are put to sleep to
free their memory and wake when opened. Only agents that can resume their conversation sleep; pin
one to keep it awake.

## Projects and the brain

The brain is one database of small linked notes shared by every project: decisions and what they
ruled out, your requirements, tech, components, problems and fixes, conventions, todos, milestones,
and rules for every agent. Fleet writes it from Claude Code and Pi agents' conversations whenever
one goes quiet, and every 2 hours while one stays busy, filing each note under the projects it
concerns (one, several or none). Agents never write it; they read it.

- A project is a folder on one machine. An agent's home project is the one whose folder holds its
  own, unless you pick another (or none) on its Edit page; it reads that project's part first.
  Agents in no project still feed the brain.
- Rules for every agent (Projects tab) are read by every agent at the start of a task. Fleet adds
  one when you tell an agent something applies everywhere; you can add, edit or remove them.
- Creating a project adds a short section to the folder's `AGENTS.md` saying how to read the brain,
  and `@AGENTS.md` to `CLAUDE.md` so Claude Code reads it too. Nothing else in those files changes.
- Agents read it with `curl` through `~/.local/state/fleet/brain.sock`: `/me/$FLEET_AGENT` (the rules
  and their project), `/search?q=`, `/n/KIND/SLUG?depth=`, `/path?from=&to=`. On the hub fleetd serves
  that socket; every other device gets the same path forwarded over ssh, so nothing is copied.
- The writer runs `claude -p` on the hub with the Claude subscription signed in there (no API key),
  or with an Anthropic or OpenAI-compatible key: Settings → Brain.
- It's stored on the hub in `~/.local/share/fleet/brain.sqlite3`, and written out as markdown in
  `~/.local/share/fleet/brain/` for Obsidian.
- The Projects tab has search, the rules, each project's notes, todos and milestones with a graph,
  a page per note (editable), and a graph of all projects joined by what they share.
