# Cross-device voice transport benchmark (runbook)

The same-host benchmark in README.md was DERP-pinned (two userspace nodes
behind one NAT can't hairpin), so it measured the worst-case relay floor.
The decisive numbers — direct-path UDP vs TCP, the ICE-TCP go/no-go — need
two physical devices on different networks. Everything is prepared; the run
is three commands and ~5 minutes.

## Setup

- **Device A (this Mac, on Wi-Fi)** — echo node.
- **Device B (second Mac, ideally tethered to the iPhone's cellular
  hotspot so the path crosses a real NAT)** — client. Build the binary
  there (`cd cli/tunneld && go build -o bin/openbase-tunneld .`) or copy it
  (Apple Silicon → Apple Silicon).
- One reusable Tailscale auth key (admin console → Settings → Keys), or
  click the interactive login URL each node prints.

## Run

Device A:

```sh
TS_AUTHKEY=tskey-auth-... ./bin/openbase-tunneld voicetest serve
# note the printed DNS name, e.g. openbase-voice-echo.tailXXXX.ts.net
```

Device B:

```sh
TS_AUTHKEY=tskey-auth-... ./bin/openbase-tunneld voicetest client \
  --host <echo-dns-name> --proto udp,tcp --duration 45s --warmup 15s
```

## Reading the results

- Confirm the path went direct, not relayed: RTT p50 should approximate a
  normal `ping` between the networks (tens of ms on cellular), not the
  ~43 ms DERP floor from the same-host run **plus** relay variance. If
  unsure, run `--duration 120s` — DERP→direct upgrade shows as a step drop
  in RTT within the first ~30 s.
- **Decision rule:** if `tsnet tcp` p95 RTT and jitter are within ~2× of
  `tsnet udp` and measured loss stays ~0%, ICE-TCP through the tunnel
  (option 1) is good enough for voice and is the simplest path. If TCP
  degrades badly relative to UDP (head-of-line blocking under loss), the
  in-app UDP relay (option 2) is required.

## Real LiveKit room join through the forwards

After the transport numbers, validate an actual room join (Device B
simulating the phone's loopback pattern):

```sh
# Device A (desktop): full daemon — forwards :18080, :7880, :7881
TS_AUTHKEY=... ./bin/openbase-tunneld serve

# Device B: phone-side loopback forwards, exactly what TailscaleKit does
TS_AUTHKEY=... ./bin/openbase-tunneld peer --host <desktop-dns> \
  --forward 17880=7880,17881=7881,18080=18080

# Device B: mint a room token through the forwarded API, then join with
# livekit-cli forced to TCP, pointing at the loopback forwards:
lk room join --url ws://127.0.0.1:17880 --token <token> <room>
```

Success criteria: participant shows connected with audio publishing, and
`lk` reports the ICE candidate pair as TCP via 127.0.0.1:17881. The LiveKit
server on Device A must have ICE-TCP enabled on :7881 (it does by default
in the Openbase Coder runtime config).
