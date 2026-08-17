# Desktop App

The Openbase Coder desktop app is the main Mac interface to the product. It
bundles the `openbase-coder` CLI runtime, walks you through first-time setup,
and then presents the dashboard: projects, coding threads, reports, approvals,
routines, skills, service health, and settings.

The dashboard pages described here are the same pages the
[web console](console.md) serves in a browser — the desktop app and console
share one UI. Electron-only additions (guided onboarding, auto-update, screen
sharing, deep links) are called out below.

Throughout this page, **On iPhone** notes describe what the
[iOS app](ios-tabs.md) can do for the same feature area.

## Install and First-Run Setup

Download the Apple Silicon DMG from [Downloads](downloads.md) and open the
app. On first run it shows a guided setup flow:

1. **Welcome** — overview of the steps ahead.
2. **Check prerequisites** — verifies macOS, the bundled CLI (activated
   automatically), and Tailscale. You can install Tailscale from here if it
   is missing (the app links the Mac App Store variant; see
   [Troubleshooting](troubleshooting.md#tailscale-login-loops-or-cli-errors-after-an-update-macos)
   for why).
3. **Set up Openbase Cloud** — starts the normal managed path: Cloud-proxied
   Claude Code plus managed speech-to-text and voice output, with free
   Openbase Cloud monthly allowances and no third-party provider keys.
4. **Voice configuration** — the normal Openbase Cloud path is already
   configured; developer/provider-key audio remains available later in
   settings.
5. **Sign in** — a browser opens for Openbase Cloud OAuth; completion is
   detected when `~/.openbase/auth.json` is written.
6. **Get Openbase on iPhone** — scan a QR code to install the iOS app and
   sign in with the same account. You can skip phone setup.
7. **Pair devices over Tailscale** — install Tailscale on both devices in the
   same tailnet, then click **Register this Mac** so the phone can find it.
   You can skip pairing.
8. **Verify** — health-checks the local API, shows CLI and app versions, and
   confirms voice configuration and login.

Prefer running these steps yourself from a terminal? Follow
[Manual Setup](manual-installation.md); the app detects completion and skips
its own setup flow.

**On iPhone:** the iOS app has a mirrored onboarding flow ("Link Your
Computer") that waits for your Mac to sign in and pair. See
[iOS App](ios-tabs.md#onboarding).

## The Dashboard

After setup the app shows a sidebar with **Workspace** and **System**
sections. Any item except Settings can be hidden via
**Settings → Sidebar Items**. The footer shows the CLI version and an update
indicator; the top bar shows the connected backend host.

### Overview

The home page. Shows up to three recent projects, your eight most recent
threads, and a warning banner (with a link to Status) if required services
are stopped.

**On iPhone:** there is no overview page; the iOS sidebar goes straight to
Call, Threads, and the other tabs.

### Projects

Browse every registered project with git branch and status badges. You can
add a project by path, search projects, expand a project to see its worktrees
and active threads, and create a new thread from a project row. Each project
row links to its Diff, Reports, and Skills.

The project detail page shows the project's threads and reports and lets you
remove the project from the recent list.

**On iPhone:** the iOS app does not browse projects directly, but the "New
thread" button on the Threads tab creates threads from recent projects.

### Threads

Threads are coding sessions. The Threads page lists them grouped by day with
color-coded status badges (running, waiting, completed, failed). You can
create a thread (choosing a project directory), star favorites, archive
threads, and jump to sync conflicts when any are detected.

The thread detail page is the live coding view: real-time output streamed
over WebSocket (stdout and stderr shown separately), a prompt box
(Enter sends, Shift+Enter inserts a newline), a Stop button to interrupt the
running turn, and the turn history.

**On iPhone:** the Threads tab has the same list (favorite via swipe-left,
archive via swipe-right), and the thread detail shows turn history, live
output, an interrupt button, and a prompt bar. During an active voice call
the thread detail also offers **Transfer Active Call** to route voice to that
thread.

### Reports

Agents write Markdown reports into per-project `.reports` folders. The
Reports page lists them across all projects grouped by date, with search and
tag filtering. Open a report to read it, tag it, download it, or delete it.

**On iPhone:** the Reports tab mirrors this — search, tag chips, date
grouping, report detail with Markdown rendering, share sheet export, and
delete. Report push notifications open the specific report.

### Dispatch

A shared chat view of the voice dispatcher thread — the routing agent that
answers voice calls and hands them to Super Agents. Use it to read what the
dispatcher is doing and to type to it directly.

**On iPhone:** the Dispatch tab shows the same dispatcher thread, and the
Call tab is where you actually speak to it. See
[Voice Routing](voice-routing.md).

### Approvals

Pending permission requests from running agents (commands, tool calls) with
Accept and Decline buttons. Auto-refreshes every 5 seconds.

**On iPhone:** the Approvals tab shows the same queue with approve/deny
buttons, and approval push notifications deep-link straight to it — so you
can unblock an agent from anywhere.

### Routines

Scheduled agent runs. Create a routine with a prompt (agent kind) or shell
command (command kind), a daily time + timezone or an interval in seconds, an
optional target thread (or a fresh thread per run), working directory, model,
and reasoning effort. Routines show their last run status and next run time,
and can be edited, disabled, run immediately, or deleted.

**On iPhone:** routines are not managed from the iOS app; use the desktop
app, the console, or `openbase-coder routines ...`.

### Skills

Browse installed agent skills and the Printing Press skill catalog. You can
search, view skill metadata (version, targets, MCP tool details), install
catalog skills to one or more targets, edit skill sources, and enable
auto-linking of personal skills into the Openbase agent homes.

**On iPhone:** skills are not managed from the iOS app.

### Templates

BoilerSync project templates. Browse templates by source repository, inspect
a template's fields (variables and options), and scaffold projects from them.

**On iPhone:** not available; templates are tied to the local filesystem.

### Status

Service health for the local runtime: each required and optional service with
its port/URL and a green (running), yellow (loaded/optional), or red
(stopped) indicator. Auto-refreshes every 30 seconds. The same information is
available from `openbase-coder services status`.

**On iPhone:** the iOS app shows a warning banner when the local runtime is
unreachable, and its Console tab can open this Status page in the embedded
browser.

### Devices

Scans your tailnet and lists Openbase hosts (name, OS, Tailscale IP, Openbase
URL, online status) alongside other tailnet devices.

**On iPhone:** Settings → Backend Host has a matching **Discover Tailnet
Hosts** action for picking which Mac the phone talks to.

### Instructions

Edit the agent instruction documents (AGENTS.md / CLAUDE.md variants) for
each environment: voice Codex home, normal Codex home, Claude config, direct
LiveKit voice sessions, Super Agents, and the dispatcher.

**On iPhone:** not available.

### Tools

Inventory of installed `uv` tools with versions, environments, and
executables; the detail page shows per-executable help and can uninstall a
tool.

**On iPhone:** not available.

### Launchctl

A manager for macOS launchd services: status, plist details, load/unload,
and logs, with Openbase-managed services flagged. An ignore list
(Settings → Ignored Launchctl) keeps unrelated services out of view.

**On iPhone:** not available.

## Settings

The Settings page groups configuration into sections:

- **Openbase Services** — start/stop/restart the managed services.
- **Coding Backend** — switch between Codex, Claude Code, and Openbase
  Cloud (same setting as `openbase-coder backend use ...`). The app warns
  before changing it, then restarts Openbase services and recreates the
  dispatcher automatically.
- **Backend Model**, **Service Tier**, **Reasoning** — model and reasoning
  defaults for agents (same as `openbase-coder defaults ...`). Service tiers
  (Fast mode) apply to the Codex backend only; on Claude Code the tier
  cannot be set and turns always run at the standard tier. Reasoning levels
  apply to both backends and are the Claude speed/quality knob.
- **LiveKit Companion Screen Sharing** (desktop app only) — see below.
- **Dispatcher Voice** — TTS/STT provider and voice selection, voice API
  keys, local model downloads, and a "Recreate LiveKit thread" action.
- **Ignored Launchctl** — services to hide from the Launchctl page.
- **Authentication** — login status and sign out.
- **Agent Instructions** — auto-generation options for instruction files.
- **Dangerous Confirmation** — toggle confirmation dialogs for destructive
  actions.
- **Sidebar Items** — show/hide sidebar entries.
- **Environment Variables** — edit env vars used by services.

**On iPhone:** iOS Settings covers the phone-side equivalents: account and
security (email, password, two-factor, sessions), backend host selection,
dispatcher voice picker, call audio behavior (mute sounds, muted-call music,
concurrent-agent threshold), and diagnostics log upload.

## Screen Sharing (LiveKit Companion)

The desktop app bundles a small companion app that can share your Mac's
display into the active LiveKit voice room, so the agent (and your phone) can
see your screen. Toggle it from **Settings → LiveKit Companion Screen
Sharing → Test Share Screen**. The first run may prompt for macOS Screen
Recording permission. When enabled, the companion can also accept remote
mouse/keyboard input sent from the iOS app over LiveKit data messages.

**On iPhone:** the shared screen is visible in the call, and the phone can
send remote-control input to the Mac.

## Auto-Update

Packaged builds check for updates automatically. When an update has
downloaded, a dismissible corner notice appears with a **Restart to update**
button; failures show a similar dismissible error notice. Development builds
never auto-update. The CLI runtime updates separately via
[self-update](commands/self-update.md), and the dashboard footer shows a
yellow dot when a CLI update is available (red when required).

**On iPhone:** iOS updates arrive through TestFlight/App Store.

## Deep Links

The app registers the `openbase-coder://` URL scheme, used for OAuth
callbacks (`openbase-coder://login-complete`) and post-subscription flows.
Links received while the app is closed are queued and handled at launch.

## Where Things Live

The app stores Electron state under
`~/Library/Application Support/@openbase/coder-desktop` and activates the
bundled CLI under `~/.openbase/packages/standalone/`. See
[Files and Paths](files-and-paths.md) for the full runtime layout and
[Uninstall](uninstall.md) for removal.
