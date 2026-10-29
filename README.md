# Fleet

A dashboard for the Claude Code sessions running on your machines.

- **fleetd** runs on every host as a systemd user service. It does the host-side work (so far, the
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

The Devices tab shows each machine's Claude sessions. Add `--show-hardware` to the hub install to
also show CPU, memory, disk and GPUs.

## Other commands

```sh
fleet status                         # fleetd, fleet-web, linger
fleet devices add NAME [--ssh DEST]  # on the hub: show another machine on the Devices tab
fleet devices remove NAME            #   (checked over your ssh login; needs only python3 there)
fleet admin reset-password EMAIL
fleet uninstall [--hub]              # fleet-web's accounts stay in its Docker volume
```
