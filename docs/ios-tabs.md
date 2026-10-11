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

On first launch, an account with no backend yet is asked where its backend should live (see [Computer or Cloud Workspace?](getting-started/computer-or-cloud-workspace.md)):

- **Set Up a New Computer** — pair the phone with a Mac running the Openbase runtime. The app directs you to `https://app.openbase.cloud` to download the Mac app and sign in, then walks through joining the private network on both devices, and waits for Mac setup to finish. Progress is detected automatically by polling your cloud account state.
- **Use a Cloud Workspace** — let Openbase Cloud host a small private workspace and pair the phone with it, with no computer at all.

An account that already has a computer running Openbase Coder or a Cloud Workspace skips this choice: the app reads that from your cloud account state and goes straight to pairing the phone.

On iOS and Android, the final onboarding step shows an estimated progress bar while the paired backend comes online. The estimate allows 90 seconds for a Cloud Workspace or 120 seconds for a computer; it is not a deadline. The bar completes when the backend answers and disappears when you leave the step.

Once this phone has completed onboarding, a sleeping Cloud Workspace keeps you in the app as long as the phone remains connected and paired. Sending a message or returning to an open conversation wakes the workspace, with a progress bar above the message box. If waking fails, the app explains why and keeps your draft for another attempt.

After onboarding, sign in with your Openbase account (email + password, with
optional two-factor authentication). The session persists in the iOS
Keychain.

**On the Mac:** the desktop app's setup flow drives the other half of this
pairing — see [Desktop App](desktop-app.md#install-and-first-run-setup).

## Chat

The app opens on an empty **new chat**, like the ChatGPT and Codex apps: the Openbase mark sits faded in the middle and a message box sits at the bottom. Every conversation, with the Dispatcher or with a coding thread, is this same screen: your messages on the right in gray bubbles, the agent's replies on the left as plain text, with copy and share under the latest reply. A reply that is still being written streams in with a **Stop** control. The Android app uses the same layout.

- A new chat starts a new coding thread. Three dropdowns sit under the message box: the **project** it works in (your recent projects), the **model** it uses, and the **device**, the computer that runs it and answers your calls. They default to what you picked last time, and choosing a device narrows the projects to that computer's. Send creates the thread on the computer that owns the project, and the screen becomes that thread. In a conversation, sending while the agent is working steers it (hold the button to queue instead).
- The **Dispatcher** is not something you start: it is one persistent conversation, always at the top of **Pinned** in the drawer. Open it there to talk to it.
- Tap the **microphone** to record a voice note. Openbase speech-to-text transcribes it into the message box so you can edit it before sending; tap again to stop. Dictation uses the STT provider selected on your backend: AssemblyAI uses its configured BYOK key, while Openbase Cloud uses your audio credits. The backend must be reachable and current; missing or rejected keys show an error without switching to Cloud. There is no on-device fallback. It stops when the app goes into the background, after ten seconds without new speech, or after five minutes. Voice notes are off while a call is running.
- With the box empty, the round **waveform** button starts a voice call.
- The title shows the conversation name. The top-right **compose** button starts a new chat; **⋯** holds the thread actions (pin, archive, transfer the active call, refresh, details). In a conversation, the **device** dropdown under the message box chooses which computer runs the next turn and answers calls.

Text you are still typing is saved on this phone separately for each conversation and for new chat, so reopening the app or switching conversations preserves your drafts. Signing out clears all saved drafts and unsent messages.

On iOS and Android, tapping **Send** immediately adds your message to the conversation and clears the message box. The bubble says **Sending…** while the app wakes the computer if needed and delivers messages in send order. When the server shows the message, its copy replaces the pending bubble. A failed send stays marked **Not sent** with a reason: choose **Retry** to send it again or **Edit** to bring it back into the message box. If a new thread was created but its first message failed, Retry uses that same thread. Messages the server has not accepted yet are kept on the phone one by one, so if the app closes they come back as **Not sent** (one that was mid-send may already have arrived, so check the conversation before retrying) (a new chat's message that never got its thread returns to the new-chat message box instead). These controls also apply to the Dispatcher and messages sent during a call.

Tap the round menu button at the top left (or swipe from the left edge) to open the side drawer; the keyboard closes when it opens. From the top:

- The Openbase mark and name (on the Openbase VPN the mark doubles as the tunnel status: solid when connected, outline when not) and a **search** button that opens the full thread list.
- Approvals, Notifications (with an unread badge), Reports, Sync, and Cloud.
- **Pinned** — the Dispatcher conversation first, then your favorite threads.
- **Recents** — your most recently updated threads, with **See all** for the full list. A green dot marks a running thread.
- A floating **New chat** button and a **Settings** gear at the bottom.

## Calls

There is no separate Call page. Start a call from the waveform button in any chat's message box. In a thread, the call starts with that thread's agent. In a new chat, choose a project and model first: the app creates a conversation on the project's computer and calls its agent without sending a text prompt. Open the pinned Dispatcher conversation to call the dispatcher.

Starting a call opens the voice view: the agent orb in the middle with the call state and the latest spoken reply under it, and the message box with **mute** and **speaker** buttons and a round **✕** to end the call. **Show messages** swaps the orb for the conversation, with a small orb in the title bar. The call banner names the call's conversation and opens its transcript when tapped. Opening another conversation shows its messages without transferring the call; text goes to the conversation you are viewing, while the orb and spoken reply follow the call. **New chat** remains available during a call. Use **Transfer call here** in a conversation's menu, including the Dispatcher, to move the active call explicitly. A conversation on another computer requires ending the current call before starting one there. The **settings** button at the top right opens the call settings: which computer answers, speaker, auto-mute and auto-unmute, room and call state, a shared screen, and **Back to Dispatch**. Other screens show the call in their top bar; tapping it opens the same settings.

While connected you can ask the dispatcher to transfer you to a Super Agent, or say "go back to dispatch" to return. The same routing is scriptable from the CLI — see [Voice Routing](voice-routing.md).

On iOS and Android, starting a call shows one estimated progress bar: a Cloud Workspace wake bar, or a 20-second estimate while waiting for the agent on a computer. It completes when the agent is ready or the wait ends, and does not restart if the agent later becomes temporarily unavailable. These estimates can take longer than expected. The services banner also waits until stopped services have been reported for at least 45 seconds; with normal 30-second polling, the warning appears on the third consecutive stopped poll, so brief restarts usually remain invisible.

Voice Test is a developer screen for exercising LiveKit connection parameters directly; it is reached only through remote app control, not the drawer.

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

The Dispatcher's conversation, opened from the top of **Pinned** in the drawer. It is the same chat screen as any thread, so you can read what the dispatcher did, steer it, and start a call from it. There is only ever one Dispatcher; it is recreated only from Settings.

## Threads

The full thread list (from the drawer's search button or **See all**), with status badges and active/loaded counts.

On Android, **Search threads** focuses an editable search field; **See all** starts with the unfiltered list. Search matches thread names, project paths, devices, and displayed statuses without regard to capitalization. It loads older pages automatically, and **Clear search** restores the loaded list. If a page fails, available results remain visible and **Retry** resumes loading; background updates preserve that pause. **Threads unavailable** means no list has loaded yet, rather than an empty search result. Opening search preserves your new-chat draft.

- **New thread** creates a thread from a recent project.
- Swipe left to favorite (pin), swipe right to archive. Pinned threads appear under **Pinned** in the drawer.
- Pull to refresh.

Tap a thread to open it as a chat. Under **⋯** → **Show details** each turn exposes its status, timestamps, return code and stderr.

On iOS and Android, conversation titles omit automatically appended thread IDs, and separate threads may share the same title. Choose **⋯** → **Copy thread ID** to copy the full identifier for a bug report or CLI command. A brief **Thread ID copied** confirmation appears below the chat header.

During an active call, a thread's **⋯** menu offers **Transfer Active Call** to route the voice session to that thread, and the call settings offer **Back to Dispatch** to hand it back.

**On the Mac:** the desktop app and console have the same thread list and
live detail view with a full keyboard.

## Sync

Resolves thread-state sync conflicts across homes and devices. Each conflict
shows the thread, the source (home vs device), the reason, and snapshot
fingerprints side by side. Device conflicts offer **Keep Local** and
**Use Remote**; home conflicts direct you to resolve from the CLI or console.

## Approvals

Pending permission requests from running agents, with approve/deny buttons and 5-second auto-refresh. The runtime creates notification feed entries for pending approvals and submits cloud pushes independently of the iPhone app. Approval alerts open the Approvals screen. The app also supports local alerts; see [Push Notifications](#push-notifications) for delivery requirements.

## Reports

Browse agent-written reports across projects: search, tag filter chips, and
date grouping (Today, This Week, This Month, Earlier). Tap a report for
rendered Markdown with previous/next navigation, a share-sheet export, and
delete. Report alerts open the specific report; the running API server discovers reports independently of whether the phone app is open.

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
- **Call Audio** — custom mute sounds and volume; **Line Static While
  Unmuted** (on by default, with its own volume), a faint phone-call static
  bed that plays only while you are unmuted on a connected call so an open
  line is audible; optional music while muted
  with many agents running (bundled "Vibes" loop, or Apple Music with an
  Openbase Cloud subscription); the concurrent-agent threshold for music
  (driven by the Brain Readiness score when available — see
  [Brain Score Concurrency](plugins/brain-score-concurrency.md)).
- **Diagnostics** — opt into **Share Anonymous Product Usage**, enable **Verbose Audio Playback Diagnostics**, or use **Upload iOS Logs**.
- **Sign Out**.

Product usage collection is off by default and requires an analytics key in the app build. When enabled, iOS sends restricted events to Amplitude for app sessions, onboarding progress/failures/skips, voice-call start/connect/end timing and outcomes, approval decisions and response timing, and diff views. Metadata includes a random persistent device ID, session and event IDs, timestamps, platform, surface, environment, and app version. Events do not include prompts, code, audio, file paths, or repository content. Turning collection off removes the stored analytics device ID.

Diagnostics are separate from product analytics. The app keeps up to 1,000 recent redacted diagnostic entries in memory, covering authentication, API/network activity, and call setup. Verbose audio diagnostics add playback timing, gaps, packet/jitter statistics, routes, and interruptions. **Upload iOS Logs**, or a connected runtime's diagnostics command, sends recent entries and the device model/OS version to the selected Mac or DevSpace, where they are saved in `~/.openbase/logs/ios-app.log`. Upload payloads redact secret-like values and email addresses.

## Push Notifications

Notifications arrive through cloud push or local app alerts:

- **Cloud push for agent announcements:** when `openbase-coder user say` finds no active voice room, the CLI submits the announcement to the authenticated Openbase Cloud endpoint. A cloud worker sends an Apple Push Notification service (APNs) alert to the account's registered iPhone token. After registration, this path does not require the iPhone app to be open. Cloud acceptance means the request was queued, not that Apple accepted it or the phone displayed it.
- **Notification feed alerts:** the running Openbase API server discovers reports, approvals, and sync conflicts at startup and every 30 seconds. Thread completions queue a background reconciliation without waiting for discovery, so report scans do not hold up turn-completion or voice UI events. New feed notifications are submitted to Openbase Cloud for push delivery, including reports written by agent threads. The server must remain running, but no phone, desktop, or web client needs to be connected. Report discovery covers all files in known projects' report directories and tracks each file independently, including new files arriving with older modification times. When report tracking is first initialized, existing reports are baselined without alerts. Manual-thread completions also submit cloud pushes.
- **App-generated alerts:** the iPhone also polls the notification feed and can schedule local alerts. The first successful refresh after launch or foregrounding reconciles the unread badge and feed quietly, without replaying the backlog. Later refreshes suppress local duplicates of notifications already delivered by APNs. This polling only runs while iOS permits the app to run; it is not required for server-side discovery or cloud push submission.
- **Held while you are on a web page:** after the computer sends your phone to a web page (`openbase-coder browser open`, `openbase-coder user phone open-url`, or a login link), thread-finished and new-report alerts created in the next five minutes wait until those five minutes are up, so a banner does not pull you out of a sign-in. They appear in the Notifications list and unread badge right away; the banner and push follow when the hold ends, and are skipped if you read the item in the meantime. Approvals and sync conflicts still alert immediately. The same hold applies on Android.

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
