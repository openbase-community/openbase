# Getting Started

This guide sets up Openbase Coder locally using the `openbase-coder` CLI.

Most users do not need this page: [download the desktop app](downloads.md)
and its guided setup runs the normal Openbase Cloud path for you, including
managed Claude Code, managed voice audio, and iPhone pairing (see
[Desktop App](desktop-app.md#install-and-first-run-setup)). Use this guide
when you prefer the terminal or are setting up a headless machine.

## Prerequisites

- macOS (`setup` and `services` use launchd) or Linux (systemd user services). The `computer-use` CLI is Linux-only for Openbase DevSpace Xorg/DCV desktops; macOS agents use native Computer Use tooling.
- Tailscale, signed in and connected, for iOS app access to the local CLI.
  On macOS, install the
  [Mac App Store variant](https://apps.apple.com/us/app/tailscale/id1475387142):
  it avoids the site-download variant's system-extension problems after
  updates (see
  [Troubleshooting](troubleshooting.md#tailscale-login-loops-or-cli-errors-after-an-update-macos)),
  and it supports everything Openbase needs. Never install both variants at
  once.
- Openbase Cloud login for the normal `openbase_cloud` backend

Openbase Coder has exactly two deployment modes:

- **Standalone (production)**: a bundled runtime package, shipped inside the
  desktop app, containing Python, the CLI, LiveKit server, a prebuilt
  console, agent instructions, and skills. It is detected automatically via
  `openbase-coder-package.json`.
- **Development**: a cloned `openbase-coder-workspace` checkout set up with the
  workspace's `./scripts/setup` script, with the CLI installed editable
  (`uv tool install -e ./cli`) or run via `uv run`.

Git, `uv`, and Node/npm are only needed for Openbase Coder development.
Plugins no longer need Node/npm: plugin console pages ship prebuilt static
assets rendered in iframes.

Local Kokoro/MLX audio is optional. When setup is run with
`--audio-provider local`, the CLI installs the local-audio Python packages into
the bundled runtime and downloads the Kokoro voices and MLX Whisper model.
Local audio needs an Apple Silicon Mac and a Python 3.12 runtime — see
[Local-Only Mode](local-only.md).

Optional developer backends:

- Codex CLI authenticated in your normal user account when using the `codex` backend
- Claude Code login for the `claude-code` backend (on macOS, setup bridges
  your normal Claude Code login into Openbase's managed config automatically
  when it can; `openbase-coder claude login` is the fallback)

## Install

There are exactly two ways to install Openbase Coder:

**Production: the Mac desktop app.** Download the Apple Silicon app from
[Downloads](downloads.md) and open it. The app bundles the CLI runtime,
activates it, and walks you through setup — see
[Desktop App](desktop-app.md). If you prefer to run the underlying setup
commands yourself in a terminal, follow
[Manual Setup for the Desktop App](manual-installation.md); it is the same
installation, operated by hand.

After setup, for fully local speech-to-text and text-to-speech:

```bash
openbase-coder setup --audio-provider local
```

**Development: the workspace checkout.** Clone the workspace repo and run
its setup script from the workspace root. It syncs the sub-repos with
`multi`, builds the console from source, and runs `openbase-coder setup`
against your checkout:

```bash
git clone --branch main --single-branch \
  https://github.com/openbase-community/openbase-coder-workspace
cd openbase-coder-workspace
./scripts/setup
```

If a standalone desktop/CLI install, or a different development workspace
install, already exists, the workspace script stops and links to
[Uninstall](uninstall.md). Uninstall first, then rerun `./scripts/setup`.

After setup completes — in either mode — authenticate with Openbase Cloud
(required for iOS app pairing and cloud onboarding):

```bash
openbase-coder login
```

Setup never clones or git-updates a workspace itself. When run without
`--workspace-dir` (and no bundled runtime package is present), it discovers
the workspace from the one recorded in `~/.openbase/installation.json`, then
from the checkout behind an editable CLI install; otherwise it errors and asks
you to clone the workspace or use the standalone install.

## First-Time Setup

What setup does:

1. Detects the bundled runtime package (standalone mode), or locates your workspace checkout (development mode).
2. Writes `~/.openbase/installation.json`.
3. Generates `~/.openbase/.env` (if it does not already exist), prompting for the coding backend when `--backend` is omitted.
4. Installs the selected backend's CLI on demand if missing (codex from GitHub release binaries into `~/.openbase/bin`, claude via Anthropic's official installer).
5. Generates Openbase instruction files from bundled or workspace templates, links Openbase Claude instructions to the generated Openbase AGENTS file, and keeps normal Claude linked to normal Codex AGENTS.
6. Symlinks bundled or workspace skills into both Openbase Codex and Claude config skill homes.
7. Downloads LiveKit agent model files (VAD, turn detector) in both modes, and initializes the CLI venv with `uv sync` in development mode.
8. Writes Codex app-server defaults such as `CODEX_MODEL=gpt-5.5`, `CODEX_MODEL_REASONING_EFFORT=high`, `CODEX_SERVICE_TIER=standard`, `CODEX_APP_SERVER_URL`, and `LIVEKIT_CODEX_THREAD_CWD`.
9. Uses the bundled console build, or builds `console` in development mode.
10. Installs background services — launchd on macOS, systemd user units on Linux (unless `--skip-services`). Backend-specific services such as `codex-app-server` are only installed for the backends that use them; visible Openbase Cloud uses Cloud-proxied Claude Code and does not install `codex-app-server`.
11. Configures Tailscale Serve routes for iOS access to the local CLI API and LiveKit:
    - `tailscale serve --bg --http=18080 http://127.0.0.1:7999`
    - `tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880`

If you do not want the Electron app to run setup commands for you, follow the
[Manual Installation](manual-installation.md) page and run the same CLI setup,
auth, service, and health-check steps from your own terminal.

## Start the Server

```bash
openbase-coder server --host 0.0.0.0 --port 7999
```

By default this command:

- Runs Django migrations
- Runs `collectstatic`
- Uses the bundled console build, or rebuilds the console in development mode
- Starts Gunicorn + Uvicorn worker(s)

## Health Check

```bash
openbase-coder doctor
openbase-coder services status
openbase-coder onboarding status
```

`onboarding status` summarizes the state the desktop/iOS onboarding flow
cares about: CLI configured, login, Tailscale identity, and Tailscale Serve
health. See [onboarding](commands/onboarding.md).

## Uninstalling Openbase

Uninstall is handled with normal system and package-manager commands, not the
`openbase-coder` CLI. Follow the [Uninstall Openbase CLI](uninstall.md) page to
stop and remove launchd/systemd services, remove the CLI package, then either
delete or archive `~/.openbase`.

## Authenticate With Openbase Cloud

```bash
openbase-coder login
```

This opens a browser OAuth flow and stores tokens in `~/.openbase/auth.json`.
It applies to both install modes and is required for iOS app pairing and
cloud onboarding; purely local use can skip it.

## Next Steps

- Tour the Mac interface in [Desktop App](desktop-app.md)
- Set up your phone with the [iOS App](ios-tabs.md) — voice calls, threads,
  approvals, reports, and diffs from anywhere
- Open the dashboard in a browser via the [Web Console](console.md)
- Learn command details in [Commands](commands/index.md)
- Install your first plugin: `openbase-coder plugins add <local-repo-or-github-url>`
- Discover bootstrap commands: `openbase-coder plugins bootstrappers`
- Run plugin bootstrap flow: `openbase-coder bootstrap <name> --params-file <file.json>`
- Review environment and auth settings in [Configuration](configuration.md)
- See all runtime artifacts in [Files and Paths](files-and-paths.md)
