# Getting Started

Openbase Coder runs as the `openbase-coder` CLI runtime on your own machine.
The recommended path starts from the GitHub workspace and keeps every part of
the install visible and editable:

### Developer Setup (recommended)

This path is designed for founders, contractors, and small-company developers
who want to keep building from anywhere with a local, inspectable Openbase
installation.

**[Developer Setup](developer-setup.md)** — start from the
[`openbase-coder-workspace`](https://github.com/openbase-community/openbase-coder-workspace)
GitHub repo and run its interactive `./scripts/setup`
from a terminal: pickers choose your coding backend and voice audio provider,
then the script walks you through Openbase Cloud login and verifies your
install. This is the strongly recommended path when you can use a terminal.
The Electron dashboard and Swift menu-bar UI are optional visual developer
surfaces after setup; Electron is never required and never owns a development
install.

### Mac App (production, no terminal)

**[Mac App Download](mac-app.md)** — use the signed Electron app released from
`main` when you want a guided production install with no terminal. It carries
the bundled CLI runtime, runs its own onboarding, and hosts the dashboard.

The developer path is fully supported: clone the
`openbase-coder-workspace` repo and run its interactive `./scripts/setup`
when you want to develop Openbase Coder itself, run from a checkout, or set
up a machine without the desktop app (for example a headless Linux box).

### Windows & Docker

On Windows, Openbase Coder runs **natively (beta)**: `./scripts/setup` works
from a Windows checkout, supervising services through the Windows service
backend instead of launchd/systemd (installer/packaging details are still
firming up). See [Developer Setup](developer-setup.md).

**[Run in Docker](../docker.md)** — the full runtime in a single Linux
container, joined to your tailnet as its own device, on any Docker engine
(macOS, Windows, or Linux). On Windows it is the most battle-tested option
today.

---

The Mac app and developer paths correspond to Openbase Coder's two
deployment modes:

- **Standalone (production)**: a bundled runtime package, shipped inside the
  desktop app, containing Python, the CLI, LiveKit server, a prebuilt
  console, agent instructions, and skills. It is detected automatically via
  `openbase-coder-package.json`.
- **Development**: a cloned `openbase-coder-workspace` checkout set up with the
  workspace's `./scripts/setup` script, with the CLI installed editable
  (`uv tool install -e ./cli`) or run via `uv run`.

Both paths run the same `openbase-coder setup` underneath and end in the same
place: a local runtime serving the console at `http://127.0.0.1:7999`,
background services managed by launchd/systemd, and private-network routes for
the phone apps.

## Prerequisites

Common to the Mac app and developer paths (the Docker path lists its own on
[Run in Docker](../docker.md#prerequisites)):

- macOS (`setup` and `services` use launchd) or Linux (systemd user services). The `computer-use` CLI is Linux-only for Openbase DevSpace Xorg/DCV desktops; macOS agents use native Computer Use tooling.
- Private networking for phone access. Production Electron onboarding offers
  **Openbase VPN** (recommended) or **Openbase Direct** when the environment
  cannot support a VPN. Developer/headless CLI installs can also use the
  expert Tailscale transport.
- Openbase Cloud login for the normal `openbase_cloud` backend

Local Kokoro/MLX audio is optional in both paths. When setup is run with
`--audio-provider local`, the CLI installs the local-audio Python packages
and downloads the Kokoro voices and MLX Whisper model. Local audio needs an
Apple Silicon Mac and a Python 3.12 runtime — see
[Local-Only Mode](../local-only.md).

## What Setup Does

Whichever path you choose, `openbase-coder setup` detects standalone vs.
development mode, writes `~/.openbase/installation.json`, generates
`~/.openbase/.env` (with the first-run backend and audio-provider pickers),
installs the selected backend's CLI if missing, renders the Openbase
instruction files, registers **only** the Super Agents MCP server and the
session-ID hook in your shared `~/.codex`/`~/.claude` homes (nothing else
there is touched), downloads the LiveKit model files, builds or uses the
console, installs the launchd/systemd services, and configures the selected
private-network transport (Openbase VPN, Openbase Direct, or the expert
Tailscale transport).

For the authoritative step-by-step phase list, every flag, and backend/audio
details, see [setup](../commands/setup.md).

## Authenticate With Openbase Cloud

After setup completes — in either mode — authenticate with Openbase Cloud:

```bash
openbase-coder login
```

This opens a browser OAuth flow and stores tokens in `~/.openbase/auth.json`.
It applies to both install modes and is required for iOS app pairing and
cloud onboarding; purely local use can skip it.

## Health Check

```bash
openbase-coder doctor
openbase-coder services status
openbase-coder onboarding status
```

`onboarding status` summarizes the state the desktop/iOS onboarding flow
cares about: CLI configured, login, private-network identity, and route
health. See [onboarding](../commands/onboarding.md).

## Uninstalling Openbase

Uninstall is handled with normal system and package-manager commands, not the
`openbase-coder` CLI. Follow the [Uninstall Openbase CLI](../uninstall.md)
page to stop and remove launchd/systemd services, remove the CLI package,
then either delete or archive `~/.openbase`.

## Next Steps

- Tour the Mac interface in [Desktop App](../desktop-app.md)
- Set up your phone with the [iOS App](../ios-tabs.md) — voice calls, threads,
  approvals, reports, and diffs from anywhere
- Open the dashboard in a browser via the [Web Console](../console.md)
- Learn command details in [Commands](../commands/index.md)
- Install your first plugin: `openbase-coder plugins add <local-repo-or-github-url>`
- Discover bootstrap commands: `openbase-coder plugins bootstrappers`
- Run plugin bootstrap flow: `openbase-coder bootstrap <name> --params-file <file.json>`
- Review environment and auth settings in [Configuration](../configuration.md)
- See all runtime artifacts in [Files and Paths](../files-and-paths.md)
