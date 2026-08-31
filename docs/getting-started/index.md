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

**[Run in Docker](../docker.md)** — the full runtime in a single Linux
container, joined to your tailnet as its own device. Because Docker Desktop
runs Linux containers on macOS and Windows, this is currently the way to
run Openbase Coder on a **Windows** machine.

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
background services managed by launchd/systemd, and Tailscale routes for the
iOS app.

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

Whichever path you choose, `openbase-coder setup`:

1. Detects the bundled runtime package (standalone mode), or locates your workspace checkout (development mode).
2. Writes `~/.openbase/installation.json`.
3. Generates `~/.openbase/.env` (if it does not already exist). On a fresh interactive install, numbered pickers choose the coding backend (when `--backend` is omitted) and the voice audio provider (when `--audio-provider` is omitted): Cloud TTS/STT, bring-your-own-keys, or local models.
4. Installs the selected backend's CLI on demand if missing (codex from GitHub release binaries into `~/.openbase/bin`, claude via Anthropic's official installer).
5. Generates Openbase instruction files from bundled or workspace templates — including the Openbase base instructions at `~/.openbase/instructions/AGENTS.md`, delivered to each Openbase session — and keeps `~/.claude/CLAUDE.md` linked to `~/.codex/AGENTS.md`.
6. Registers the super-agents MCP server and the session-ID hook in your shared agent homes (`~/.codex/config.toml`, `~/.claude.json`, `~/.claude/settings.json`) and symlinks bundled or workspace skills into `~/.codex/skills` and `~/.claude/skills`. Nothing else in the shared homes is touched — Openbase sessions use your own Codex and Claude Code logins and settings.
7. Downloads LiveKit agent model files (VAD, turn detector) in both modes, and initializes the CLI venv with `uv sync` in development mode.
8. Writes Codex app-server defaults such as `CODEX_MODEL_REASONING_EFFORT=high`, `CODEX_SERVICE_TIER=standard`, the standard Unix `CODEX_APP_SERVER_URL` on macOS/Linux, and `LIVEKIT_CODEX_THREAD_CWD`.
9. Uses the bundled console build, or builds `console` in development mode.
10. Installs background services — launchd on macOS, systemd user units on Linux (unless `--skip-services`). Backend-specific services such as `codex-app-server` are only installed for the backends that use them; visible Openbase Cloud uses Cloud-proxied Claude Code and does not install `codex-app-server`.
11. Configures the selected private-network transport and its API/LiveKit
    routes. Openbase VPN uses bundled Netmesh networking: an Openbase-operated
    Headscale control plane plus Tailscale-compatible open-source clients. It
    does not require a Tailscale account. It collects no VPN traffic or usage
    analytics and sends no VPN analytics to Tailscale,
    and its full-device route lets a phone browser open websites created on
    the computer. Openbase Direct embeds only the Openbase connection, so it
    works without a VPN but does not expose those sites to other phone apps.

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
