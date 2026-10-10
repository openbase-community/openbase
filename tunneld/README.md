# openbase-tunneld (tsnet prototype)

Embeds a Tailscale node ([tsnet](https://tailscale.com/kb/1244/tsnet)) inside
the Openbase Coder runtime so users don't need to install the Tailscale app or
create a Tailscale account. See the "Onboarding v2: Embedded Tailscale (tsnet)"
plan in Notion for the full architecture.

## What it replaces

| v1 (Tailscale app required)                          | v2 (this daemon)                     |
| ---------------------------------------------------- | ------------------------------------ |
| `tailscale serve --http=18080 http://127.0.0.1:7999` | tailnet `:18080` reverse proxy       |
| `tailscale serve --tcp=7880 tcp://127.0.0.1:7880`    | tailnet `:7880` TCP forward          |
| LiveKit media direct to tailnet IP `:7881/:7882`     | tailnet `:7881` ICE-TCP forward      |
| `tailscale status --json` subprocess calls           | `GET http://127.0.0.1:7998/status`   |
| host-network HTTP probes of tailnet peers            | `GET http://127.0.0.1:7998/probe`    |

The `/status` payload is `ipnstate.Status`, the same JSON schema that
`tailscale status --json` prints, so existing CLI parsers work unchanged.
The authenticated `/probe` endpoint is intentionally limited to the Openbase
health path on port `18080` of peers present in the node's current tailnet
status; it is not a general-purpose tailnet proxy.

## Dynamic forwards

Cloud workspaces (Maritime) and other embedded-node hosts can expose one loopback port at a time-limited tailnet address through the control API, for a dev server the user wants to open on a phone or a loopback callback server (`openbase-coder service expose PORT`). `openbase-coder browser open URL` currently delivers the login URL only; automatic callback forwarding is not available yet.

| Call                               | Effect                                                                 |
| ---------------------------------- | ---------------------------------------------------------------------- |
| `POST /forwards` `{port, ttl_seconds?, one_shot?, peer?}` | listen on tailnet `:port`, forward to `127.0.0.1:port` |
| `GET /forwards`                    | live forwards with their expiry and connection counts                  |
| `DELETE /forwards/{port}`          | close one forward                                                      |

Rules: ports 1024-65535 only, never the fixed ports above or TURN port 3478, at most 16 live forwards, default TTL 10 minutes and at most an hour. `one_shot` retires the forward after its first completed connection (an OAuth callback is one request). Expiry, removal, and one-shot retirement close active connections as well as the listener. `peer` pins the forward to one device, given as a tailnet IP or stable node id; both forms require a successful WhoIs lookup matching the remote address. Every forward listens on the tailnet only; reachability is already limited to the same account's devices by the Openbase VPN policy. All three routes require the control token, and unknown request fields are rejected so a misspelled pin cannot silently create an unpinned forward.

## Build

```sh
cd tunneld
go build -o bin/openbase-tunneld .
```

## Run the desktop daemon

```sh
# With a pre-provisioned auth key (the production plan: cloud mints these):
TS_AUTHKEY=tskey-auth-... ./bin/openbase-tunneld serve

# Or without a key: start it, then open the interactive login URL from
# `curl -s http://127.0.0.1:7998/health | jq -r .auth_url`.
```

State lives in `~/.openbase/tsnet` (`--statedir` / `OPENBASE_TSNET_STATE_DIR`).
Point at a self-hosted control plane (e.g. Headscale) with `--control-url` /
`OPENBASE_TSNET_CONTROL_URL`.

## Enable in the Python CLI

```sh
export OPENBASE_TSNET=1
export OPENBASE_TUNNELD_BIN=/path/to/bin/openbase-tunneld  # or put it on PATH
```

With the flag set, `configure_tailscale_serve()` starts the daemon instead of
configuring `tailscale serve`, and identity/peer lookups in
`services/tailnet_devices.py` go through the control API. Without the flag,
nothing changes.

## End-to-end demo (no Tailscale app anywhere)

1. Start the Openbase Coder runtime as usual (Django on `127.0.0.1:7999`).
2. Start the daemon with an auth key (above) and note the node's DNS name:
   `curl -s http://127.0.0.1:7998/health | jq -r .self_dns_name`
3. Simulate the phone from any machine — a second embedded node, no Tailscale
   app involved (ephemeral; it removes itself on exit):

   ```sh
   TS_AUTHKEY_PEER=tskey-auth-... ./bin/openbase-tunneld peer \
     --url http://<self_dns_name>:18080/api/health/
   ```

   Expected: `GET ... -> 200` with `{"status": "ok"}` — the same health check
   the iOS app performs during pairing.

## Phone-side loopback forwards (Option 1 plumbing)

The `peer` subcommand can hold open loopback forwards through the embedded
node — the same mechanism TailscaleKit's loopback proxy uses on iOS:

```sh
./bin/openbase-tunneld peer --host <desktop-dns> --forward 17880=7880,17881=7881
```

A LiveKit client pointed at `ws://127.0.0.1:17880` then signals through the
tailnet, and ICE-TCP media dialed to `127.0.0.1:17881` rides the tailnet to
the desktop's LiveKit `:7881`.

## Voice media measurements

`voicetest` sends synthetic voice frames (default 50 fps × 160 B, the
LiveKit/opus cadence) between two embedded nodes and reports RTT, jitter,
and loss for both transports under consideration:

```sh
# Echo node (desktop side):
TS_AUTHKEY=... ./bin/openbase-tunneld voicetest serve

# Measure (phone side): --proto udp (in-app relay option) or tcp (ICE-TCP option)
TS_AUTHKEY=... ./bin/openbase-tunneld voicetest client --host <echo-dns> --proto udp

# Loopback baseline without tsnet:
./bin/openbase-tunneld voicetest serve --direct 127.0.0.1:19100
./bin/openbase-tunneld voicetest client --direct 127.0.0.1:19100 --proto udp
```

### Measured results (2026-07-10, both nodes on one Mac)

45 s at 50 fps × 160 B, 25 s warmup excluded, real tailnet:

| Transport            | RTT p50 | RTT p95 | Jitter (mean) | Loss  |
| -------------------- | ------- | ------- | ------------- | ----- |
| loopback baseline    | 0.3 ms  | 0.4 ms  | 0.05 ms       | 0%    |
| tsnet UDP            | 43 ms   | 125 ms  | 8.1 ms        | 0%    |
| tsnet TCP            | 44 ms   | 126 ms  | 8.2 ms        | 0.4%  |

Interpretation:

- Two userspace nodes on the *same host* cannot hairpin a direct path, so
  this traffic rode a DERP relay — treat these numbers as the **worst-case
  floor**, not the expected phone↔desktop path.
- Even fully relayed, sustained voice cadence saw zero UDP loss and ~8 ms
  jitter; ~21 ms added one-way is within the ~150 ms mouth-to-ear budget.
- TCP ≈ UDP here because DERP itself is a TCP relay. The direct-path
  UDP-vs-TCP comparison (the real ICE-TCP question) requires two physical
  devices; the harness is ready for that run.

## Limitations

- The voicetest harness quantifies transport quality, but a real LiveKit
  room join with forced ICE-TCP through the forwards (and the client-SDK
  wiring to prefer `127.0.0.1:17881`) is still to be proven.
- Same-host node pairs pin to DERP (no hairpin); cross-device runs are the
  meaningful benchmark.
- The daemon binds its control API to loopback and requires a bearer capability
  stored in `<statedir>/control.token` with mode `0600`. Processes running as
  the same Unix user remain inside the same local trust boundary.
- Electron (`desktop/electron/main.cjs`) still shells out to the Tailscale
  binary for its own identity display; wiring it to `/status` is a follow-up.

### Phone CLI callback relay

`browser open` uses the Python `openbase_coder_cli.callback_relay` child process for OAuth callbacks. It exposes a random relay port through this daemon; it does **not** expose the CLI's callback port. The relay snapshots a loopback-only listener's PID, process creation time, file descriptor, host and port, plus its Linux socket identity, checks that it belongs to the invoking user, and rechecks the listener before forwarding traffic. Loss of that listener stops new connections; existing exchanges may finish within the original deadline while their owning process remains alive. If a CLI closes its listener on accept, the accepted socket must still belong to the original process before any browser bytes are sent. Cancellation, process exit, or the deadline closes all connections and removes the dynamic forward. The daemon TTL also limits exposure if the child crashes.

The existing `loopback_forward.token` / `forward_token` field carries `OBR1_<relay-port>_<unix-expiry>_<random-capability>`, with a 24-byte random URL-safe suffix. This keeps old app-control and Cloud push validators compatible. New native clients validate the structure and VPN target, connect to that relay port over VPN, send `OPENBASE-LOOPBACK/1 <complete-token>\n`, and require `OK\n` before sending browser bytes. The relay strips this handshake; HTTP/TLS bytes and the original localhost Host header remain unchanged. Tokens, URLs containing authorization codes, and callback bodies must not be logged.

A health handshake and browser preconnect never connect to the real CLI listener. Dynamic forwarding uses `one_shot=false`: the capability handshake is itself a two-way exchange, and some login flows serve a localhost page before the actual callback. The Python relay owns completion and permits multiple HTTP connections while the same CLI listener remains live. Both sides bound connection counts and enforce the original deadline; duplicate native requests cannot extend it or replace another live capability on the same localhost port. A `started` acknowledgement means native listeners and relay health are ready, not that external authorization succeeded.
