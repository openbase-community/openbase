# Openbase Coder in Docker

Runs the `openbase-coder` runtime — the local Django API, the LiveKit voice
stack (server + agent worker), sync workers, and routines — inside a single
Linux container, with **Tailscale as the networking layer**, exactly like a
native install: phones and other devices reach the runtime over the tailnet
via `tailscale serve` (18080 → 7999 API, 7880 → 7880 LiveKit signaling), and
LiveKit advertises the tailnet address for media.

This is an internal/development mechanism, not a supported installation
pathway (see the workspace `GLOSSARY.md`). Use it for headless testing,
CI-style smoke checks, and experimenting with the runtime on machines where
a native install is impractical.

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

`openbase-coder login` starts its OAuth callback listener on a random
loopback port *inside the container*, but the browser redirect lands on the
loopback of the machine running the browser. Userspace tailscaled forwards
inbound tailnet TCP to the container's loopback, so bridge the redirect over
the tailnet: note the `127.0.0.1:<port>` in the printed authorize URL, run a
loopback forwarder on the browser machine
(`socat TCP-LISTEN:<port>,bind=127.0.0.1,fork TCP:<container-tailnet-ip>:<port>`),
open the URL, then stop the forwarder.

The runtime is reachable at `http://<hostname>.<tailnet>.ts.net:18080/api/health/`
on the tailnet (the entrypoint logs the exact URL) and at
`http://localhost:7999/api/health/` on the docker host.

Any arguments to `docker run` bypass the supervisor and exec directly, e.g.
`docker run --rm openbase-coder:local openbase-coder --help`.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `TS_AUTHKEY` | — | Tailscale auth key for unattended tailnet joins. |
| `TS_HOSTNAME` | `openbase-coder` | Tailnet device hostname. |
| `TS_SOCKET` | — | Use an external tailscaled (sidecar) socket instead of starting one in-container. |
| `OPENBASE_CODER_NETWORK_MODE` | `tailscale` | Set `local` for loopback-only testing with no Tailscale. |
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
tailnet address; userspace netstack forwards it to loopback, but this path
has not yet been verified with a live phone call — if media fails in
userspace mode, use the kernel TUN or sidecar variants above.

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
  (e.g. `-v ~/Projects:/home/openbase/Projects`).
