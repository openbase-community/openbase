# Run in Docker

The Docker image runs the full Openbase Coder runtime — the local API, the
LiveKit voice stack, sync workers, and routines — in a single Linux
container, on any Docker engine (macOS, **Windows**, or Linux). On Windows,
Openbase Coder also runs natively in beta (`./scripts/setup` from a Windows
checkout — see [Developer Setup](getting-started/developer-setup.md)); the
Docker image is the most battle-tested Windows option today.

Tailscale is the networking layer, exactly like every other install: the
container joins your tailnet as its own device, and from the apps' point of
view it is just another backend host. Once it is on your tailnet, the
[iOS app](ios-tabs.md) adds it under **Settings → Backend Host** like a Mac,
and the [web console](console.md) is reachable at
`http://openbase-coder.<your-tailnet>.ts.net:18080`.

For a native Mac development server with one HTTP port, prefer
[`openbase-coder service publish`](commands/service.md). A multi-port Compose
project is the exception: join the container/backend to the tailnet as described
here, or put one HTTP ingress in front of the containers. Do not treat a single
published URL as covering independent HTTP, database, and UDP ports.

## Prerequisites

The image includes the dispatcher and Super Agent instructions plus the bundled agent skills. Setup renders the instructions into the persistent data directory and links the skills into both coding backends' agent homes. Every container boot refreshes managed instruction files before launching the application services, so upgrading an image also restores missing files and applies template updates on an existing data volume. Django service startup also refreshes these files, including after an in-place desktop runtime upgrade. Unmarked custom dispatcher, Super Agent, and voice instruction files remain unchanged in workspace installations; standalone installations regenerate their packaged defaults. Openbase's generated base instructions are refreshed in both modes.

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (macOS, Windows, or Linux) or any Docker engine: `brew install --cask docker` on macOS, `winget install Docker.DockerDesktop` on Windows, or `curl -fsSL https://get.docker.com | sh` for Docker Engine on Linux.
- A free [Tailscale](https://tailscale.com) account, with the Tailscale app installed on the phone or computer you will connect from: `brew install --cask tailscale` on macOS, `winget install Tailscale.Tailscale` on Windows, `curl -fsSL https://tailscale.com/install.sh | sh` on Linux, or Tailscale from the App Store or Google Play on a phone.
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

The login prints a browser URL whose final redirect targets
`http://127.0.0.1:52807/...` — an address that lives inside the container
(the callback port is always `52807`, so you can even set the bridge up
before starting the login). Bridge that port from the machine whose browser
you use, over the tailnet (find the container's address with
`docker exec openbase-coder tailscale ip -4`):

- macOS / Linux:
  `socat TCP-LISTEN:52807,bind=127.0.0.1,fork TCP:<container-tailnet-ip>:52807`
- Windows (PowerShell as Administrator):
  `netsh interface portproxy add v4tov4 listenport=52807 listenaddress=127.0.0.1 connectport=52807 connectaddress=<container-tailnet-ip>`
  (remove it afterwards with `netsh interface portproxy delete v4tov4
  listenport=52807 listenaddress=127.0.0.1`)

Then open the login URL, finish signing in, and restart the container once so
every service picks up the new credentials:

```sh
docker restart openbase-coder
```

The bridge is for finishing a login in a desktop browser. To sign in from your phone instead, the image sets `BROWSER` and `GH_BROWSER` to [`openbase-coder browser open`](#sign-in-from-your-phone), which sends a login page to the Openbase app on your phone (or prints the URL when the app cannot be reached).

### Sign in from your phone

GitHub CLI is included in the image. First check `gh auth status --hostname github.com` as the workspace user and reuse the intended account if already signed in. Otherwise run `gh auth login --web --hostname github.com --git-protocol https`, keep it running, and enter its device code at `https://github.com/login/device` on your phone. The agent should promptly include the code and next action in its final chat reply and speak the code during an active voice session; opening the page alone is not enough. If the command only prints the URL, send it with `openbase-coder browser open https://github.com/login/device`. After approval, verify with `gh auth status --hostname github.com` and `gh api user --jq .login`. The default `~/.config/gh` directory is persisted on the data volume. GitHub’s device-code flow needs no localhost callback; other tools may need the forwarding or paste-back flow below.

When the phone acknowledges a callback-forwarding request, `browser open` reports whether forwarding started. If the phone's Openbase VPN is off, turn it on and retry. If forwarding fails, is unsupported, or is not confirmed, use the paste-back flow below. A missing or unrecognized result does not confirm that forwarding is available.

Agents in the container follow the bundled `openbase-cli-logins` skill: they prefer device-code and paste-code logins (`gh auth login`, `codex login --device-auth`, `openbase-coder claude login`, `gcloud auth login --no-launch-browser`), which need no bridge at all, and use `openbase-coder browser open <url>` to put any other login page on your phone. On a cloud workspace, `browser open` also prepares an authenticated relay for the login's `localhost:<port>` callback on the workspace's VPN address for ten minutes and asks the phone to forward its own loopback port there, so a login that redirects to `localhost` completes without any bridge once your phone app supports forwarding. When a login still ends on a `http://localhost:<port>/...` page that fails to load on the phone, use the Openbase app's "Paste login link" action (it sends the address to the container, which replays it), or copy that full address and paste it back to the agent in the same thread; the agent replays it with `openbase-coder browser replay`. The address holds a single-use code that expires within minutes, so paste it only there.

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

Managed Super Agent thread IDs, names, the Dispatcher session, and the tracked-project registry persist in the data volume across image replacements. Keep that volume attached when upgrading. Containers running images from before this persistence change need a one-time snapshot of their home-directory agent state before their first upgrade; follow the container migration procedure in the source repository's `docker/README.md` before replacing the old container.

The container defaults to the Openbase Cloud backend. To use native Codex or
Claude Code instead, log in *inside the container* — do not copy credential
files in from another machine (copied logins break when the provider rotates
refresh tokens). Logins persist in the `openbase-data` volume.

Claude Code (no port bridging needed):

```sh
docker exec -it openbase-coder openbase-coder claude login
```

Open the printed URL in any browser, sign in, and paste the code it shows
back into the terminal. `openbase-coder claude status` confirms the login.

Codex:

```sh
docker exec -it openbase-coder codex login
```

Codex waits for a browser redirect to `http://localhost:1455/...`, which
lives inside the container — bridge port `1455` from your browser machine
over the tailnet exactly like the [Openbase login](#log-in-to-openbase)
above, then open the printed URL. Openbase services read the shared
`~/.codex/auth.json` directly; no re-setup is needed.

`codex login --device-auth` needs no bridge: open the printed URL on any device and enter the code it shows.

Then switch the backend and restart:

```sh
docker exec -it openbase-coder openbase-coder backend use claude_code   # or codex
docker restart openbase-coder
```

(Use the CLI + `docker restart` rather than the console's backend setting
inside Docker — the console's automatic service restart relies on
launchd/systemd, which the container does not run.)

## Notes and limits

- On a graceful stop, the entrypoint signals services to exit, flushes filesystem buffers immediately, then flushes again after services finish. Allow enough stop time for service shutdown and disk I/O. A forced kill or a VM halt that bypasses the entrypoint cannot guarantee that recent writes are durable.
- The entrypoint attempts to shorten Linux page-cache writeback using `OPENBASE_DIRTY_EXPIRE_CENTISECS` (default `500`) and `OPENBASE_DIRTY_WRITEBACK_CENTISECS` (default `100`). Values are in hundredths of a second and must contain only digits. Maritime applies these settings before dropping root privileges; an ordinary unprivileged container usually cannot change them and continues with the current kernel settings. This reduces exposure to abrupt stops but does not guarantee durability.
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
