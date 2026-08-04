# iOS App

The Openbase iOS app is the phone client for Openbase Coder. It is where you
hold voice calls with the dispatcher and Super Agents, follow and steer coding
threads, approve agent requests, read reports, and review diffs — all against
the local runtime that the [desktop app](desktop-app.md) or `openbase-coder`
CLI runs on your Mac (or a [Cloud DevSpace](cloud-devspace.md)).

Get it from [Downloads](downloads.md). The app connects over Tailscale to the
CLI server, LiveKit server, and agent services started by
`openbase-coder setup` and `openbase-coder services ...`.

## Onboarding

On first launch the app offers two paths:

- **Link Your Computer** (default) — pair the phone with a Mac running the
  Openbase runtime. The app directs you to `https://app.openbase.cloud` to
  download the Mac app and sign in, then walks through installing Tailscale
  on both devices (same tailnet), and waits for Mac setup to finish. Progress
  is detected automatically by polling your cloud account state.
- **Start with Cloud** — skip pairing and use Openbase Cloud from the phone.

After onboarding, sign in with your Openbase account (email + password, with
optional two-factor authentication). The session persists in the iOS
Keychain.

**On the Mac:** the desktop app's setup flow drives the other half of this
pairing — see [Desktop App](desktop-app.md#install-and-first-run-setup).

## Navigation

Swipe from the left edge or tap the menu button to open the sidebar. It has a
**Workspace** section (Call, Voice Test, Dispatch, Threads, Sync, Approvals,
Reports, Diff, Console, Cloud) and a **System** section (Settings).

## Call

The primary voice interface. Tap to start a LiveKit call with the
dispatcher — the routing agent that can start, resume, and hand you to Super
Agents by voice. The top bar shows the call state (calling, connected agent
name, or error), with mute/unmute and end-call buttons. The screen shows an
audio visualizer, live agent activity, and the latest agent message.

While connected you can ask the dispatcher to transfer you to a Super Agent,
or say "go back to dispatch" to return. The same routing is scriptable from
the CLI — see [Voice Routing](voice-routing.md).

Voice Test is a developer screen for exercising LiveKit connection
parameters directly.

**Action Button mute shortcut:** the app exposes an App Intent named
`Toggle Voice Session Mute` (shortcut title `Toggle Mute`). Create an iOS
Shortcut that runs it and assign it to the iPhone Action Button to mute or
unmute the active voice session from the hardware button. Supported phrases
include "Toggle Openbase mute" and "Toggle voice session mute in Openbase".
It has no effect when no call is active.

**On the Mac:** the desktop app's Dispatch page shows the dispatcher thread
as text chat, and its screen-sharing companion can publish the Mac's display
into the same call.

## Dispatch

A read-and-steer view of the dispatcher's thread: its turn history, current
turn, and reasoning, with an interrupt button. Auto-refreshes every 15
seconds.

## Threads

Lists your coding threads with status badges and active/loaded counts.

- **New thread** creates a thread from a recent project.
- Swipe left to favorite, swipe right to archive.
- Pull to refresh.

Tap a thread for the detail view: connection indicator, expandable turn
history (prompt, status, timestamps, output, stderr, Markdown rendering),
the live current turn with real-time output over WebSocket, an interrupt
button, and a prompt bar to send the next message.

During an active call, thread detail shows **Transfer Active Call** to route
the voice session to that thread, and a **Return to Dispatch** action to hand
it back.

**On the Mac:** the desktop app and console have the same thread list and
live detail view with a full keyboard.

## Sync

Resolves thread-state sync conflicts across homes and devices. Each conflict
shows the thread, the source (home vs device), the reason, and snapshot
fingerprints side by side. Device conflicts offer **Keep Local** and
**Use Remote**; home conflicts direct you to resolve from the CLI or console.

## Approvals

Pending permission requests from running agents, with approve/deny buttons
and 5-second auto-refresh. Approval push notifications deep-link here, so you
can unblock an agent without being at your Mac.

If your phone loses connectivity while an agent is waiting for approval, the
agent stays blocked at the approval checkpoint. The request is stored in the
local approval queue on the Mac and reappears in the Approvals tab when the
iPhone reconnects over Tailscale. Openbase does not roll back already completed
work; the agent can only continue from the last confirmed state after you
approve. Skill-originated approval prompts may include a timeout (the CLI
default is 5 minutes), in which case the waiting action is cancelled if no
answer arrives in time.

## Reports

Browse agent-written reports across projects: search, tag filter chips, and
date grouping (Today, This Week, This Month, Earlier). Tap a report for
rendered Markdown with previous/next navigation, a share-sheet export, and
delete. Report push notifications open the specific report.

## Diff

A mobile-optimized git diff viewer for your repositories, served by the local
console at `/mobile/diff` and embedded in the app. The app injects your CLI
auth token automatically.

## Console and Cloud

- **Console** opens the local web dashboard (`http://<host>:18080`) in the
  embedded browser — the full [console](console.md), including the Status
  page.
- **Cloud** opens `https://app.openbase.cloud`, your Openbase Cloud account.

## Settings

- **Account & security** — email addresses, password, two-factor
  authentication, active sessions, connected accounts.
- **Backend Host** — pick which Mac (or DevSpace) the app talks to. Add
  backends by Tailscale DNS name or IP, or use **Discover Tailnet Hosts**.
  Each backend row shows its computed URLs (`http://<host>:18080` for the
  API, `ws://<host>:7880` for LiveKit).
- **Dispatcher Voice** — choose the dispatcher's voice and recreate the
  dispatcher thread to apply it.
- **Call Audio** — custom mute sounds and volume; optional music while muted
  with many agents running (bundled "Vibes" loop, or Apple Music with an
  Openbase Cloud subscription); the concurrent-agent threshold for music
  (driven by the Brain Readiness score when available — see
  [Brain Score Concurrency](plugins/brain-score-concurrency.md)).
- **Diagnostics** — upload iOS logs to support.
- **Sign Out**.

## Push Notifications

The app routes notifications to the right screen:

- Approval requests → Approvals tab
- New reports → Reports tab (opens the specific report)
- Thread sync conflicts → Sync tab

Thread turn start/completion events refresh the UI in place.

## How the App Connects

The selected backend host is a Tailscale DNS name, IP address, or hostname.
The app builds these runtime URLs from it:

- Codex/Openbase API: `http://<host>:18080`
- LiveKit signaling: `ws://<host>:7880`

For iPhone access over Tailscale, the local setup must expose the CLI API and
LiveKit ports from the Mac:

- `18080` forwards to the local Django/Openbase API on `127.0.0.1:7999`.
- `7880` forwards to the local LiveKit server on `127.0.0.1:7880`.
- LiveKit media uses TCP `7881` and UDP `7882`.

`openbase-coder setup` configures these Tailscale Serve routes; verify with:

```bash
openbase-coder doctor
openbase-coder services status
```

If a call reaches the room token endpoint but hangs during LiveKit
connection, see [Troubleshooting](troubleshooting.md) for the Tailscale and
LiveKit listener checks.

Local persistence: the CLI auth token lives in the Keychain
(`com.openbase.coder.cli.authtoken`); backend hosts and the selected host are
in UserDefaults (`openbase_agent_hosts`, `openbase_selected_host_id`).
