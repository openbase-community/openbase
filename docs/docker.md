# Run in Docker

The Docker image runs the full Openbase Coder runtime — the local API, the
LiveKit voice stack, sync workers, and routines — in a single Linux
container. Because Docker Desktop runs Linux containers on macOS and
**Windows**, this is currently the way to run Openbase Coder on a Windows
machine.

Tailscale is the networking layer, exactly like every other install: the
container joins your tailnet as its own device, and from the apps' point of
view it is just another backend host. Once it is on your tailnet, the
[iOS app](ios-tabs.md) adds it under **Settings → Backend Host** like a Mac,
and the [web console](console.md) is reachable at
`http://openbase-coder.<your-tailnet>.ts.net:18080`.

## Prerequisites

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS,
  Windows, or Linux) or any Docker engine.
- A free [Tailscale](https://tailscale.com) account, with the Tailscale app
  installed on the phone or computer you will connect from.
- An Openbase account for the default Openbase Cloud coding backend and
  voice audio.

## Start the container

```sh
docker run -d --name openbase-coder --hostname openbase-coder \
  -p 7999:7999 \
  -v openbase-data:/home/openbase/.openbase \
  openbaseai/openbase
```

All state — your logins, the Tailscale identity, settings, and the local
database — lives in the `openbase-data` volume, so it survives container
restarts and image upgrades.

## Join your tailnet

```sh
docker exec -it openbase-coder tailscale up
```

Open the printed URL and approve the device. Services and the tailnet routes
recover automatically after login — no restart needed. (For unattended
setups, pass `-e TS_AUTHKEY=tskey-auth-...` to `docker run` instead, using an
[auth key](https://login.tailscale.com/admin/settings/keys).)

## Log in to Openbase

```sh
docker exec -it openbase-coder openbase-coder login
```

The login prints a browser URL whose final redirect targets a
`http://127.0.0.1:<port>/...` address that lives inside the container. Bridge
that port from the machine whose browser you use, over the tailnet (find the
container's address with `docker exec openbase-coder tailscale ip -4`):

- macOS / Linux:
  `socat TCP-LISTEN:<port>,bind=127.0.0.1,fork TCP:<container-tailnet-ip>:<port>`
- Windows (PowerShell as Administrator):
  `netsh interface portproxy add v4tov4 listenport=<port> listenaddress=127.0.0.1 connectport=<port> connectaddress=<container-tailnet-ip>`
  (remove it afterwards with `netsh interface portproxy delete v4tov4
  listenport=<port> listenaddress=127.0.0.1`)

Then open the login URL, finish signing in, and restart the container once so
every service picks up the new credentials:

```sh
docker restart openbase-coder
```

## Use it

- Console: `http://openbase-coder.<your-tailnet>.ts.net:18080`
- iOS app: **Settings → Backend Host** → your container's tailnet name.
  Voice calls — including call audio — work over the tailnet.
- Local API health (on the Docker host): `http://localhost:7999/api/health/`

Coding sessions operate on the container's filesystem. Mount the projects
you want agents to work on:

```sh
docker run -d --name openbase-coder --hostname openbase-coder \
  -p 7999:7999 \
  -v openbase-data:/home/openbase/.openbase \
  -v "$HOME/Projects:/home/openbase/Projects" \
  openbaseai/openbase
```

## Codex and Claude Code backends

The container defaults to the Openbase Cloud backend. To use native Codex or
Claude Code instead, log in *inside the container* — do not copy credential
files in from another machine (copied logins break when the provider rotates
refresh tokens). Logins persist in the `openbase-data` volume.

Claude Code (no port bridging needed):

```sh
docker exec -it openbase-coder openbase-coder claude login
```

Open the printed URL in any browser, sign in, and paste the code it shows
back into the terminal. `openbase-coder claude status` confirms the scoped
login.

Codex:

```sh
docker exec -it openbase-coder codex login
```

Codex waits for a browser redirect to `http://localhost:1455/...`, which
lives inside the container — bridge port `1455` from your browser machine
over the tailnet exactly like the [Openbase login](#log-in-to-openbase)
above, then open the printed URL. The service picks the login up through its
auth symlink; no re-setup is needed.

Then switch the backend and restart:

```sh
docker exec -it openbase-coder openbase-coder backend use claude_code   # or codex
docker restart openbase-coder
```

(Use the CLI + `docker restart` rather than the console's backend setting
inside Docker — the console's automatic service restart relies on
launchd/systemd, which the container does not run.)

## Notes and limits

- Service status in the console reflects the container's supervisor, but the
  console's service start/stop buttons do not apply inside Docker — restart
  the container instead.
- Voice calls run over the tailnet like any install, including call audio
  (verified with real phone calls against the default unprivileged
  networking mode). Advanced networking variants (kernel TUN, Tailscale
  sidecar) are described in the
  [image documentation](https://github.com/openbase-community/openbase/tree/develop/docker).
- Choose a different coding backend or bring-your-own voice keys with
  `-e OPENBASE_CODER_BACKEND=...`, `-e OPENBASE_CODER_AUDIO_PROVIDER=...`,
  `-e ASSEMBLY_AI_API_KEY=...`, and `-e CARTESIA_API_KEY=...` on the first
  `docker run`.
