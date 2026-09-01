# Openbase Coder

Openbase Coder is a voice-first AI coding product. You talk to coding agents
from your iPhone, Android phone, or Mac while a local runtime on your computer
runs the actual coding sessions against your repositories.

These docs cover the whole product, not just the CLI:

- **[Desktop app](desktop-app.md)** — the macOS Electron app. Guided setup,
  and the main dashboard for projects, threads, reports, approvals, routines,
  skills, settings, and screen sharing.
- **[iOS app](ios-tabs.md)** — voice calls with the dispatcher and Super
  Agents, thread management, approvals, reports, and diffs from your phone.
- **Android app** — the same phone client for Android (Kotlin/Compose),
  mirroring the iOS workflow: voice calls, threads, approvals, reports, and
  diffs. Get the APK from [Downloads](downloads.md).
- **[Web console & Openbase Cloud](console.md)** — the same dashboard in a
  browser, plus your account at `https://app.openbase.cloud`. Openbase Cloud's
  own Heroku-style CLI (`openbase`) is documented separately at
  [docs-cloud.openbase.cloud](https://docs-cloud.openbase.cloud).
- **`openbase-coder` CLI** — the local runtime underneath all of the above: a
  Django API + WebSocket server, LiveKit voice services, and launchd/systemd
  service management. See [Commands](commands/index.md).

## Which Page Do I Need?

- Installing for the first time → [Getting Started](getting-started/index.md):
  [download the Mac app](getting-started/mac-app.md), use the
  [developer setup](getting-started/developer-setup.md), or run it in
  [Docker (including on Windows)](docker.md).
- What can I do in the Mac app? → [Desktop App](desktop-app.md)
- What can I do on my phone? → [iOS App](ios-tabs.md) (the Android app mirrors it)
- What is app.openbase.cloud for? → [Web Console & Cloud](console.md)
- Talking to agents by voice, transferring calls → [Voice Routing](voice-routing.md)
- Something is broken → [Troubleshooting](troubleshooting.md)
- CLI flags and behavior → [Commands](commands/index.md)

## Quick Start

When you can use a terminal, start with the recommended
[Developer Setup](getting-started/developer-setup.md): clone the workspace repo
and run its interactive setup script:

```bash
git clone --branch main --single-branch \
  https://github.com/openbase-community/openbase-coder-workspace
cd openbase-coder-workspace
./scripts/setup
```

The Electron dashboard and Swift menu-bar app are optional visual surfaces
after developer setup. For a production install with no terminal, instead
[download the Mac app](downloads.md) and follow its guided setup. It installs
the bundled CLI, signs you in, and pairs your phone over Openbase VPN or
Openbase Direct. [Manual Setup for the Desktop App](manual-installation.md)
documents the same standalone setup when you want to operate it from a
terminal.

## Documentation

Using the apps:

- [Desktop App](desktop-app.md)
- [iOS App](ios-tabs.md)
- [Web Console & Openbase Cloud](console.md)
- [Voice Routing](voice-routing.md)

Setup and operations:

- [Getting Started](getting-started/index.md)
  ([Mac App Download](getting-started/mac-app.md) ·
  [Developer Setup](getting-started/developer-setup.md))
- [Downloads](downloads.md)
- [Manual Setup](manual-installation.md)
- [Run in Docker](docker.md)
- [Cloud DevSpace](cloud-devspace.md)
- [Local-Only Mode](local-only.md)
- [Steering Codex TUI Sessions](codex-tui-steering.md)
- [Troubleshooting](troubleshooting.md)
- [Uninstall](uninstall.md)

Reference:

- [Commands](commands/index.md)
- [Configuration](configuration.md)
- [Files and Paths](files-and-paths.md)
- [Release](release.md)
