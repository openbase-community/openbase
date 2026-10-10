# Troubleshooting

Where problems surface in the apps: the [desktop app](desktop-app.md) and
[console](console.md) show service health on their **Status** page and a
warning banner on the Overview page; the [iOS app](ios-tabs.md) shows a
warning banner when the local runtime is unreachable and can upload logs from
**Settings → Diagnostics**. The checks below are the CLI-side diagnosis for
the most common failures.

## Setup Before Openbase VPN Connects

Openbase VPN enrolls during sign-in and pairing, after setup installs the local services. Until a VPN address and interface are available, LiveKit starts on loopback so setup can finish. A background worker checks for the address every 15 seconds and restarts LiveKit, its voice agent, and the local API once the VPN is ready, deferring while a voice call is active. If that transition fails, it retries no sooner than 10 minutes later; the pending transition remains until every service restart completes. This also handles a VPN that was unavailable when an already-paired computer started.

If desktop setup fails, its error identifies the failed step. Finish resolving that error before continuing to sign-in; a service startup failure is not a request to sign in. Check local services with `openbase-coder services status`.

## iPhone Stays On Connecting

The iOS app reaches the Mac through Tailscale Serve, not directly through the
local Django port. The expected routes are:

```bash
tailscale serve --bg --http=18080 http://127.0.0.1:7999
tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880
```

Check the configured routes:

```bash
tailscale serve status
```

Then check local service health:

```bash
openbase-coder doctor
openbase-coder services status
```

Both commands should fail if either Serve route is missing or the Openbase API
health check cannot be reached through the machine's tailnet `:18080` address.

## iPhone LiveKit Call Times Out Over Tailscale

Symptoms:

- The iOS app can reach the local CLI API.
- `POST /api/livekit-room-token/` returns `200`.
- The app logs a LiveKit URL such as `ws://<machine>.tailnet-name.ts.net:7880`.
- The LiveKit agent joins the room, but the iPhone fails during `room.connect` or times out before publishing the microphone.

This usually means signaling is working but WebRTC media cannot complete ICE. One known cause is LiveKit advertising the machine's Tailscale IP while its UDP media socket is only bound to loopback.

Check the local LiveKit listeners:

```bash
lsof -nP -iTCP:7880 -iTCP:7881 -iUDP:7882
```

For Tailscale iPhone calls, LiveKit should have UDP listeners on both loopback and the machine's Tailscale addresses, for example:

```text
UDP 127.0.0.1:7882
UDP 100.x.y.z:7882
UDP [fd7a:115c:a1e0::...]:7882
TCP *:7881 (LISTEN)
TCP 127.0.0.1:7880 (LISTEN)
```

If UDP is only bound on `127.0.0.1:7882`, regenerate and reload the launchd service wrappers from a version of `openbase-coder` that includes the Tailscale interface fix:

```bash
openbase-coder services regenerate
openbase-coder services install
```

Then restart the LiveKit services:

```bash
openbase-coder restart --service livekit-server
openbase-coder restart --service livekit-agent
```

Openbase Coder also heals stale voice-agent state on its own in the background: if the agent's pre-warmed pool goes stale after sleep/wake and a call would otherwise stall, it detects and recycles the agent automatically. This manual restart stays available for when a call is failing right now.

For new installs, `openbase-coder setup` generates the corrected LiveKit wrapper automatically. Existing installs need regenerated wrappers because launchd runs the generated shell scripts in `~/.openbase/launchd/`.

The corrected wrapper derives `LIVEKIT_INTERFACE` from the interface that owns `LIVEKIT_NODE_IP`, rather than trusting a route lookup while Tailscale is still settling. You can still override the values in `~/.openbase/.env` when needed:

```bash
LIVEKIT_NETWORK_MODE=tailscale
LIVEKIT_NODE_IP=100.x.y.z
LIVEKIT_INTERFACE=utunN
LIVEKIT_BIND_IP=127.0.0.1
LIVEKIT_TCP_PORT=7881
LIVEKIT_UDP_PORT=7882
```

## Tailscale Login Loops or CLI Errors After an Update (macOS)

Symptoms, usually right after a Tailscale app update or a macOS security
update, with the site-download (standalone) Tailscale variant:

- Tailscale shows "Authentication In Progress" or "Waiting for Network..."
  forever, or "Unable to add a new user. Please try again."
- Every CLI command fails, even `tailscale down`:
  `The Tailscale CLI failed to start: ... (Tailscale.CLIError error 1.)`
- In Openbase, the iPhone sticks on connecting while the Mac's backend is
  otherwise healthy.

This is a known failure state of the standalone variant's macOS system
extension; uninstalling and reinstalling the same variant often does not
recover it. Switch to the Mac App Store variant, which uses a sandboxed
network extension and avoids this class of breakage:

1. Fully uninstall the current Tailscale following
   [Tailscale's uninstall steps](https://tailscale.com/kb/1153/uninstall),
   then reboot. Never leave both variants installed at once.
2. Install [Tailscale from the Mac App Store](https://apps.apple.com/us/app/tailscale/id1475387142)
   and sign in to the same tailnet.
3. Re-run setup from the desktop app (or `openbase-coder setup`) so the
   Tailscale Serve routes are configured again, then verify with
   `tailscale serve status` and `openbase-coder doctor`.

The App Store variant supports everything Openbase uses (port-mode
Tailscale Serve, the bundled CLI, MagicDNS). The site download remains an
option for machines without App Store access; the one conflict to know
about in the App Store variant is Apple's Screen Time web filter.

## Voice Route Exit Returns 502 With Invalid LiveKit URL

Symptoms:

- `POST /api/livekit-voice-route/exit/` returns `502 Bad Gateway`.
- The Django log contains `ValueError: Invalid URL: port can't be converted to integer`.
- The bad URL contains Tailscale CLI error text, for example `http://The Tailscale CLI failed to start: ...:7880/...`.

This means the Django launchd service started while `tailscale ip -4` returned an error string instead of an IPv4 address, and that string was captured into `LIVEKIT_URL`.

Regenerate wrappers and restart Django:

```bash
openbase-coder services regenerate
openbase-coder restart --service django-cli
```

The service wrapper validates the derived Tailscale IPv4 address before exporting `LIVEKIT_URL`. If Tailscale cannot provide a valid IPv4 address in `tailscale` mode, the service now exits with a clear startup error instead of running with a malformed LiveKit URL.

## Enable iOS Auth Diagnostics

iOS keeps a small redacted `AuthDiagnostics` buffer in memory for the Upload iOS Logs action. Verbose console printing is disabled by default. Enable it only while debugging auth, CLI API, or LiveKit call setup.

You can enable it from code with:

```swift
AuthDiagnostics.setEnabled(true)
```

Or set the process environment variable in an Xcode scheme:

```text
OPENBASE_AUTH_DIAGNOSTICS=1
```

Upload payloads redact secret-like values and email addresses before they are written to the local runtime log directory. Do not leave verbose console diagnostics enabled for routine development sessions unless you need the extra local output.

## Codex CLI Warns About an Older Background Service

Symptom: launching `codex` prints "A background Codex service is running
vX, older than your Codex CLI vY".

Openbase runs Codex as long-lived `codex-app-server` services. After Codex is
upgraded (for example with `npm install -g @openai/codex`, or when a new Node
version gets its own global install), those services keep the old binary in
memory until they restart.

This only applies when the background service is *older* than the CLI. The
opposite case (a background server newer than the CLI, typically Codex's own
self-updating daemon) is covered by [Codex Says the Background Server Has
Incompatible Feature Settings](#codex-says-the-background-server-has-incompatible-feature-settings);
Openbase never restarts anything for it.

Openbase handles the stale-service case itself:

- The console health banner shows "Service 'codex-app-server' is running
  Codex X, but Codex Y is installed" with a **Restart** button.
- `openbase-coder services status` prints the same mismatch next to the
  service.
- The `sync-workers` service restarts the affected services automatically
  once nothing is in flight: no Super Agents turn, no voice call, and no
  conversation attached to the app-server (an interactive `codex` chat)
  that is mid-turn, waiting on an approval, or was active in the last ten
  minutes. An open `codex` tab that has been idle longer than that does not
  hold the restart back; it loses its connection and `codex resume` brings
  the conversation back. Set `OPENBASE_CODEX_AUTO_RESTART=0` in
  `~/.openbase/.env` to keep the warning but never restart automatically;
  `OPENBASE_CODEX_RECENT_THREAD_SECONDS` changes the ten-minute window.

To restart by hand: `openbase-coder restart --service codex-app-server`
(and `--service codex-app-server-dispatcher`). This briefly interrupts
running Codex threads and any voice call.

## Codex Says the Background Server Has Incompatible Feature Settings

Symptom: every new `codex` session opens a dialog, "Background server has
incompatible feature settings", listing a few feature flags and offering
**1. Run without daemon this time** or **2. Restart with these settings**.
`codex app-server daemon version` prints a `cliVersion` lower than its
`appServerVersion` (for example `0.160.1` against `0.161.0`).

Cause: Codex's managed app-server daemon updates itself hourly, while the
Codex CLI on your `PATH` is only upgraded when you (or Openbase) reinstall
it. When a Codex release changes a shared feature default, an older CLI
computes different required features than the newer daemon serves and
refuses to attach. The daemon is Codex's, not Openbase's: it owns the
standard control socket (`~/.codex/app-server-control/app-server-control.sock`
is a symlink into the daemon's runtime directory), so Openbase's own
`codex-app-server` service stays idle beside it instead of starting a second
server. `openbase-coder services status` shows this as
`codex-app-server available through the shared Codex daemon (<version>)`
followed by the version mismatch, and the console health banner shows
"Codex CLI X is older than the shared Codex daemon Y; upgrade the Codex CLI
to Y" without a restart button.

Fix: upgrade the Codex CLI to the daemon's version. This swaps files on
disk only; the daemon and every attached session keep running.

```bash
npm install -g @openai/codex@<daemon version>
codex app-server daemon version   # cliVersion must now equal appServerVersion
```

The Openbase upgrade warning clears on the next health check. If Openbase's
dispatcher app-server is still on the old version afterwards, the
[stale-service handling above](#codex-cli-warns-about-an-older-background-service)
restarts it once nothing is in flight.

Until you upgrade, choose **1. Run without daemon this time**. That session
runs on its own and cannot be steered from Openbase, but nothing else is
affected.

Never choose **2. Restart with these settings** on a shared daemon. It
restarts the daemon with the older CLI's feature values persisted, which
disconnects every Codex session attached to it (Openbase Super Agents, other
`codex` terminals, and any voice call in progress) and leaves the daemon on
non-default settings that newer clients then fight over. Openbase never
restarts the shared daemon for the same reason.

## Codex Resume Fails Against the Shared App-Server

Three distinct failures can block `codex --remote unix:// resume <name>`
(resuming a session on the Openbase-managed Codex app-server by name):

**"Cannot verify a unique session label across server pages; matching
session UUID: …"** — Codex resolves a session name by paging the server's
active interactive thread list 100 at a time, and it refuses every name
match — even a unique one — when the listing spans more than one page.
Openbase names each dispatched agent thread, so active installs accumulate
thousands of threads and trip this permanently. Archive the stale ones:

```bash
openbase-coder threads archive-stale --dry-run   # inspect first
openbase-coder threads archive-stale             # archive threads idle > 10 days
```

Archiving is reversible: archived threads stay resumable by UUID and can be
unarchived. The command reports `resumeByNameUsable`; the active interactive
set must fit one page (100 threads or fewer). As a one-off workaround,
resume by the UUID printed in the error message.

**"Permission overrides are not supported when resuming a remote task."** —
the invocation carries a permission-override flag (approval policy or
sandbox). A thread resumed over an explicit `--remote` endpoint keeps the
permission settings it was started with on the server, so drop the override
flags from the `resume` or `fork` invocation; nothing is lost.

**"No saved session found with ID `<word>`"** for a name that exists — the
session name is a single positional argument with exact matching
(`codex resume <name> [prompt]`). A two-word invocation passes only the
first word as the name and the rest as the opening prompt; quote the name
or use its exact hyphenated form.
