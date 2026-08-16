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

## Notes and limits

- Service status in the console reflects the container's supervisor, but the
  console's service start/stop buttons do not apply inside Docker — restart
  the container instead.
- Voice calls signal over the tailnet like any install. If call audio does
  not flow on your network, see the advanced networking variants in the
  [image documentation](https://github.com/openbase-community/openbase/tree/develop/docker)
  (kernel TUN and Tailscale sidecar modes).
- Choose a different coding backend or bring-your-own voice keys with
  `-e OPENBASE_CODER_BACKEND=...`, `-e OPENBASE_CODER_AUDIO_PROVIDER=...`,
  `-e ASSEMBLY_AI_API_KEY=...`, and `-e CARTESIA_API_KEY=...` on the first
  `docker run`.
