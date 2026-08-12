# Openbase Coder

Openbase Coder is a voice-first AI coding product. You talk to coding agents
from your iPhone or Mac while a local runtime on your computer runs the actual
coding sessions against your repositories.

These docs cover the whole product, not just the CLI:

- **[Desktop app](desktop-app.md)** — the macOS Electron app. Guided setup,
  and the main dashboard for projects, threads, reports, approvals, routines,
  skills, settings, and screen sharing.
- **[iOS app](ios-tabs.md)** — voice calls with the dispatcher and Super
  Agents, thread management, approvals, reports, and diffs from your phone.
- **[Web console & Openbase Cloud](console.md)** — the same dashboard in a
  browser, plus your account at `https://app.openbase.cloud`. Openbase Cloud's
  own Heroku-style CLI (`openbase`) is documented separately at
  [docs-cloud.openbase.cloud](https://docs-cloud.openbase.cloud).
- **`openbase-coder` CLI** — the local runtime underneath all of the above: a
  Django API + WebSocket server, LiveKit voice services, and launchd/systemd
  service management. See [Commands](commands/index.md).

## Which Page Do I Need?

- Installing for the first time → [Getting Started](getting-started.md) or
  just [download the desktop app](downloads.md) and follow its setup flow.
- What can I do in the Mac app? → [Desktop App](desktop-app.md)
- What can I do on my phone? → [iOS App](ios-tabs.md)
- What is app.openbase.cloud for? → [Web Console & Cloud](console.md)
- Talking to agents by voice, transferring calls → [Voice Routing](voice-routing.md)
- Something is broken → [Troubleshooting](troubleshooting.md)
- CLI flags and behavior → [Commands](commands/index.md)

## Quick Start

The easiest path is the desktop app: [download the Mac app](downloads.md),
open it, and follow the guided setup. It installs the bundled CLI, walks you
through the Openbase Cloud managed setup path, signs you in for the free trial,
and pairs your iPhone over Tailscale.

Prefer to drive the terminal yourself? Install and open the desktop app —
it activates the bundled CLI runtime automatically — then follow
[Manual Setup for the Desktop App](manual-installation.md) to run the same
setup, login, and service steps by hand.

For source development, clone the workspace repo and run its setup script:

```bash
git clone https://github.com/openbase-community/openbase-coder-workspace
cd openbase-coder-workspace
./scripts/setup
```

## Documentation

Using the apps:

- [Desktop App](desktop-app.md)
- [iOS App](ios-tabs.md)
- [Web Console & Openbase Cloud](console.md)
- [Voice Routing](voice-routing.md)

Setup and operations:

- [Getting Started](getting-started.md)
- [Downloads](downloads.md)
- [Manual Setup](manual-installation.md)
- [Cloud DevSpace](cloud-devspace.md)
- [Local-Only Mode](local-only.md)
- [Troubleshooting](troubleshooting.md)
- [Uninstall](uninstall.md)

Reference:

- [Commands](commands/index.md)
- [Configuration](configuration.md)
- [Files and Paths](files-and-paths.md)
- [Release](release.md)
