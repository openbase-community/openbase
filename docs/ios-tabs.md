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

Pending permission requests from running agents, with approve/deny buttons and 5-second auto-refresh. The iPhone app polls for requests and generates local alerts for newly detected requests. These alerts open the Approvals screen; they are not cloud push notifications and cannot detect new requests while the app is suspended or stopped. The initial load populates the list without alerting for existing requests.

## Reports

Browse agent-written reports across projects: search, tag filter chips, and
date grouping (Today, This Week, This Month, Earlier). Tap a report for
rendered Markdown with previous/next navigation, a share-sheet export, and
delete. Locally generated report alerts open the specific report; detection requires the app's report monitor to be running.

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
  Once a DevSpace has positively identified itself as an Openbase Cloud
  Workspace, starting a call automatically resumes it after idle shutdown;
  real machines never trigger Cloud startup.
- **Dispatcher Voice** — choose the dispatcher's voice and recreate the
  dispatcher thread to apply it.
- **Call Audio** — custom mute sounds and volume; optional music while muted
  with many agents running (bundled "Vibes" loop, or Apple Music with an
  Openbase Cloud subscription); the concurrent-agent threshold for music
  (driven by the Brain Readiness score when available — see
  [Brain Score Concurrency](plugins/brain-score-concurrency.md)).
- **Diagnostics** — opt into **Share Anonymous Product Usage**, enable **Verbose Audio Playback Diagnostics**, or use **Upload iOS Logs**.
- **Sign Out**.

Product usage collection is off by default and requires an analytics key in the app build. When enabled, iOS sends restricted events to Amplitude for app sessions, onboarding progress/failures/skips, voice-call start/connect/end timing and outcomes, approval decisions and response timing, and diff views. Metadata includes a random persistent device ID, session and event IDs, timestamps, platform, surface, environment, and app version. Events do not include prompts, code, audio, file paths, or repository content. Turning collection off removes the stored analytics device ID.

Diagnostics are separate from product analytics. The app keeps up to 1,000 recent redacted diagnostic entries in memory, covering authentication, API/network activity, and call setup. Verbose audio diagnostics add playback timing, gaps, packet/jitter statistics, routes, and interruptions. **Upload iOS Logs**, or a connected runtime's diagnostics command, sends recent entries and the device model/OS version to the selected Mac or DevSpace, where they are saved in `~/.openbase/logs/ios-app.log`. Upload payloads redact secret-like values and email addresses.

## Push Notifications

There are two notification delivery paths:

- **Cloud push for agent announcements:** when `openbase-coder user say` finds no active voice room, the CLI submits the announcement to the authenticated Openbase Cloud endpoint. A cloud worker sends an Apple Push Notification service (APNs) alert to the account's registered iPhone token. After registration, this path does not require the iPhone app to be open. Cloud acceptance means the request was queued, not that Apple accepted it or the phone displayed it.
- **App-generated alerts for approvals, reports, and sync conflicts:** monitors in the iPhone app poll the selected runtime and schedule local notifications. They cannot detect new events while iOS suspends the app or the app is stopped. These monitors do not provide cloud push delivery.

Opening the signed-in app requests or retries remote-notification registration. This registration step is distinct from receiving later APNs alerts. Notification permission, a valid registered token, and successful cloud/APNs delivery are required for cloud alerts; presentation remains subject to iOS notification settings.

The app routes alerts to the right screen:

- Approval requests → Approvals tab
- New reports → Reports tab (opens the specific report)
- Thread sync conflicts → Sync tab
- Cloud agent announcements → the linked thread

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
