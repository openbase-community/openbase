# Openbase Coder in Docker

Runs the `openbase-coder` runtime — the local Django API, the LiveKit voice
stack (server + agent worker), sync workers, and routines — inside a single
Linux container, with **Tailscale as the networking layer**, exactly like a
native install: phones and other devices reach the runtime over the tailnet
via `tailscale serve` (18080 → 7999 API, 7880 → 7880 LiveKit signaling), and
LiveKit advertises the tailnet address for media.

The published image is `openbaseai/openbase` on Docker Hub (linux/amd64 +
linux/arm64). It backs the Maritime-managed DevSpaces launched from
openbase.cloud and is the easy way to try Openbase Coder in a container on
any Docker host. (Windows hosts run the native Windows install, not this
image.) User-facing instructions live in `docs/docker.md` (published at
docs.openbase.cloud); this file is the deeper image/development reference.

## Publishing

CI publishes `openbaseai/openbase` (linux/amd64 + linux/arm64) via
`.github/workflows/docker-image.yml` on pushes to `main` that touch the
image inputs, tagging `latest` plus a version derived from the newest `v*`
tag. It needs the `DOCKERHUB_TOKEN` repo secret (a Docker Hub access token
for the `openbaseai` account); without it the workflow warns and skips.

## Build

From the repo root:

```sh
docker build -t openbase-coder:local .
# Optionally stamp a real version (hatch-vcs has no git metadata in-context):
docker build --build-arg OPENBASE_CODER_VERSION=1.2.3 -t openbase-coder:1.2.3 .
```

Or with compose: `docker compose up --build`.

## Run

```sh
docker run -d --name openbase-coder --hostname openbase-coder \
  -e TS_AUTHKEY=tskey-auth-... \
  -p 7999:7999 \
  -v openbase-data:/home/openbase/.openbase \
  openbase-coder:local
```

(`--hostname` matters: an interactive `tailscale up` names the tailnet node
after the container hostname, which is otherwise a random container ID.)

On first start the entrypoint:

1. starts `tailscaled` inside the container (unprivileged, userspace
   networking; node identity persists in the volume) and joins the tailnet —
   with `TS_AUTHKEY` if provided, otherwise run
   `docker exec -it openbase-coder tailscale up` and follow the login URL
   (services and serve routes recover automatically after login);
2. performs a non-interactive `openbase-coder setup` into the `~/.openbase`
   volume (random secrets, coding-backend binary, pinned `livekit-server`);
3. configures the `tailscale serve` routes (18080 → 7999, 7880 → 7880) and
   supervises the same per-service wrapper scripts a launchd/systemd install
   runs.

Then authenticate with Openbase Cloud:

```sh
docker exec -it openbase-coder openbase-coder login
docker restart openbase-coder   # services pick up the machine token
```

`openbase-coder login` starts its OAuth callback listener on loopback port
`52807` *inside the container* (the port is fixed, so the bridge can be set
up before the login even starts), but the browser redirect lands on the
loopback of the machine running the browser. Userspace tailscaled forwards
inbound tailnet TCP to the container's loopback, so bridge the redirect over
the tailnet: run a loopback forwarder on the browser machine
(`socat TCP-LISTEN:52807,bind=127.0.0.1,fork TCP:<container-tailnet-ip>:52807`),
open the URL, then stop the forwarder.

The runtime is reachable at `http://<hostname>.<tailnet>.ts.net:18080/api/health/`
on the tailnet (the entrypoint logs the exact URL) and at
`http://localhost:7999/api/health/` on the docker host.

Any arguments to `docker run` bypass the supervisor and exec directly, e.g.
`docker run --rm openbase-coder:local openbase-coder --help`.

## Maritime-managed mode

Openbase Cloud can run this image as a private Maritime Workspace. This is a
control-plane-managed mode, not a manual `docker run` login flow:

- Cloud sets `OPENBASE_CODER_RUNTIME=maritime`, mounts the durable provider
  disk at `/data`, and sets `OPENBASE_CODER_CLI_DATA_DIR=/data/openbase`.
  Maritime mode defaults the network mode to `netmesh`, so the box joins the
  per-user Openbase netmesh via the embedded `openbase-tunneld` and needs no
  published ports at all: the API (tailnet `:18080`), LiveKit signaling
  (`:7880`), ICE-TCP media (`:7881`), and the embedded TURN relay (`:3478`)
  are all served outbound-first from the node.
- Backend logins for both coding backends survive container recreation: the
  entrypoint keeps `~/.codex` and `~/.claude` (plus the Claude state file,
  via `CLAUDE_CONFIG_DIR`) inside the data volume.
- The image refuses Maritime startup as root or with state outside `/data`.
- Cloud supplies a short-lived, single-use bootstrap grant. The runtime
  exchanges it for an installation-scoped machine token with only
  `llm_proxy` and `audio_proxy` scopes and a non-reusable Netmesh key.
- The runtime never receives a host `auth.json`, owner access/refresh token,
  reusable Tailscale key, or Maritime provider token.
- The API remains on loopback and `publicWeb` remains disabled. Embedded
  `openbase-tunneld` is the only private-network forwarder.
- Stopping preserves `/data`. Explicit Workspace termination revokes the
  machine/Netmesh identities before Cloud deletes the provider agent and disk.

Do not set the bootstrap variable manually or copy credential files into a
Maritime volume. The complete control-plane contract, threat model, and safe
migration procedure are in `docker/maritime-security.md`.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `TS_AUTHKEY` | — | Tailscale auth key for unattended tailnet joins. |
| `TS_HOSTNAME` | `openbase-coder` | Tailnet device hostname. |
| `TS_SOCKET` | — | Use an external tailscaled (sidecar) socket instead of starting one in-container. |
| `OPENBASE_CODER_NETWORK_MODE` | `tailscale` (`netmesh` under Maritime) | `tailscale`, `netmesh` (embedded `openbase-tunneld`; alias `netmesh-tsnet`), or `local` for loopback-only testing. |
| `TS_AUTHKEY` (netmesh) | — | Netmesh pre-auth key for unattended enrollment; normally staged by `provision` from the bootstrap exchange instead of passed directly. The staged key is deleted only after the node reports an enrolled, forwarding state. |
| `OPENBASE_TSNET_CONTROL_URL` | `https://net.openbase.cloud` | Netmesh headscale control plane for `openbase-tunneld`. |
| `OPENBASE_TSNET_HOSTNAME` | derived | Stable tailnet hostname for the netmesh node (e.g. `devspace-<id>`). |
| `OPENBASE_TSNET_STATE_DIR` | `<data>/tsnet` | Netmesh node identity/state; keep below the data dir so it survives image upgrades. |
| `OPENBASE_CODER_CLI_PORT` | `7999` | Local API bind port; `openbase-tunneld`'s tailnet `:18080` forward follows it. |
| `OPENBASE_CODER_WORKSPACE_DIR` / `OPENBASE_CODER_PROJECTS_DIR` | `/data/workspace` (Maritime) | Durable projects dir for coding sessions; in Maritime mode it must live below `/data` (`OPENBASE_CODER_WORKSPACE_DIR` wins only when it points below `/data`). |
| `OPENBASE_CODER_RUNTIME` | — | Cloud sets `maritime` only for a managed private Maritime Workspace. |
| `OPENBASE_CODER_CLI_DATA_DIR` | `~/.openbase` | Durable state root; managed Maritime mode requires a path below `/data`. |
| `OPENBASE_CODER_BACKEND` | `openbase-cloud` | Coding backend for first-run setup (`codex`, `claude-code`, `openbase-cloud`). |
| `OPENBASE_CODER_AUDIO_PROVIDER` | `openbase-cloud` | Voice audio provider for first-run setup (`openbase-cloud`, `cartesia`; `local` is Apple-Silicon-only). |
| `ASSEMBLY_AI_API_KEY` / `CARTESIA_API_KEY` | — | Bring-your-own audio keys for the `cartesia` audio provider. |
| `OPENBASE_CODER_SERVICES` | all default services | Space-separated service list to supervise (e.g. `django-cli` alone). |
| `OPENBASE_CODER_CLI_ALLOWED_HOSTS` | `*` | Django allowed hosts, written into the env file. Tighten when publishing beyond localhost. |

Container-appropriate values are rewritten into `~/.openbase/.env` between
`# BEGIN docker overrides` / `# END docker overrides` markers on every start,
so mode changes take effect on `docker restart`.

## How Tailscale runs

- **In-container tailscaled, userspace networking (default).** Runs as the
  unprivileged `openbase` user with `--tun=userspace-networking`; no
  capabilities or devices required. Inbound tailnet traffic is proxied by
  tailscaled's netstack to loopback, which is why the entrypoint pins
  `LIVEKIT_INTERFACE=lo` in this mode. State lives at
  `~/.openbase/tailscale/` in the volume, so the node identity survives
  container recreation. A `tailscale` CLI shim in `~/.openbase/bin` points
  every product call (service wrappers, serve health, `docker exec`) at the
  in-container daemon socket.
- **Kernel TUN (optional).** If the container runs as root with
  `--cap-add NET_ADMIN --device /dev/net/tun`, the entrypoint drops the
  userspace flag and tailscaled creates a real `tailscale0` interface —
  closest to a native install for WebRTC media.
- **Sidecar (optional).** Run the official `tailscale/tailscale` image with
  `network_mode: service:openbase-coder`, share its socket directory into
  this container, and set `TS_SOCKET` to that socket path; the entrypoint
  then manages no daemon of its own.

Signaling and the API go over `tailscale serve` (TCP), which is fully
supported in userspace mode. LiveKit **media** is UDP to port 7882 on the
tailnet address; userspace netstack forwards it to loopback, and this path
is verified working with real phone calls. The kernel TUN and sidecar
variants above remain available for networks where userspace forwarding
falls short.

In **netmesh** mode there is no tailscaled at all: `openbase-tunneld` (an
embedded tsnet node) serves the tailnet forwards itself, and media never
depends on inbound UDP — it rides ICE-TCP (tailnet `:7881`) or the embedded
TURN relay (tailnet `:3478`). LiveKit advertises loopback-only candidates
(the loopback UDP socket exists solely for the TURN relay's host-local
allocation sockets), so mesh clients never wait on an unreachable UDP
candidate.

## Limitations

- **Console tracks remote refs, not the local checkout**: the web console is
  cloned from the public console/coder-react/multi-react/boilersync-react
  repos during the image build (`CONSOLE_REF` etc. build args, default
  develop/main), so a locally modified console is not what gets baked in.
- **No systemd/launchd**: services are supervised by the entrypoint script
  with a restart-on-exit loop. Service *status* (console health banners,
  `services status`) is accurate — the image sets
  `OPENBASE_CODER_SERVICE_SUPERVISOR=external` and the entrypoint maintains
  `~/.openbase/run/<name>.pid` files — but start/stop/restart actions are
  not functional; restart the container instead.
- **Coding sessions operate on the container filesystem**: mount the
  projects you want agents to work on
  (e.g. `-v ~/Projects:/home/openbase/Projects`). Node (with npm/corepack)
  and git are bundled for agent tooling; other toolchains must be installed
  into the container.
- **Code-sync works**: syncthing is bundled (pinned to the version
  `code_sync/install.py` would download). After enabling sync from the
  console, restart the container so the entrypoint starts the code-sync
  service.
