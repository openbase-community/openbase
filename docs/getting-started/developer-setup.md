# Developer Setup

Openbase is designed for founders, contractors, and small-company developers
who need their coding environment available on the go. The GitHub repository
is the developer entry point; the setup script below owns the installation.

Installing from the GitHub workspace is the strongly recommended, fully
supported install path, with
an interactive terminal flow: run `./scripts/setup` with no flags and it
picks your coding backend and voice audio provider, walks you through
Openbase Cloud login, and verifies the install. Use it when you want to
develop Openbase Coder itself, run the runtime from source, or set up a
machine without the desktop app (for example a headless Linux box you
administer over SSH). (Just want the product on a Mac? See
[Mac App Download](mac-app.md). On Windows, `./scripts/setup` runs natively in
beta — or use the [Docker image](../docker.md), the most battle-tested Windows
option today.)

## Prerequisites

In addition to the shared [prerequisites](index.md#prerequisites),
development installs need:

- Git: `xcode-select --install` on macOS, or your distribution's package on Linux (for example `sudo apt install git`)
- [`uv`](https://docs.astral.sh/uv/): `curl -LsSf https://astral.sh/uv/install.sh | sh`
- [`multi`](https://pypi.org/project/multi-workspace/) 3.2.21 or newer, which setup uses to sync the sub-repos: `uv tool install multi-workspace` (or `uv tool upgrade multi-workspace`)
- Go: `brew install go` on macOS, or the current toolchain from [go.dev/dl](https://go.dev/dl/). Setup builds the Openbase Direct transport from source, so Go is required whichever transport you pick.
- Node 20+ (`brew install node`, or `pnpm env use --global 22`) and pnpm (`curl -fsSL https://get.pnpm.io/install.sh | sh -`) for building the console from source

`./scripts/setup` checks for all of these before it does anything else and exits with the install command for each one that is missing.

If `pnpm --version` already works (for example from `npm install -g pnpm` or Corepack), keep that pnpm and skip installing another. A second copy only conflicts: `brew install pnpm` then stops with a link error because `pnpm` already exists in the Homebrew `bin` directory. That error is harmless, since setup uses whichever pnpm is on your `PATH`; to switch to the Homebrew copy instead, run `brew link --overwrite pnpm`.

`uv tool install` puts `multi` in `~/.local/bin`, which is not on the `PATH` of a fresh Mac. If uv warns about that, run `uv tool update-shell` and open a new terminal, or run `export PATH="$HOME/.local/bin:$PATH"` in the current one. Otherwise setup reports `multi` as missing even though it is installed.

Contributors who commit to the repos also need [`gitleaks`](https://github.com/gitleaks/gitleaks) (`brew install gitleaks`). Setup does not need it, but the git hooks that `multi sync` installs refuse commits and pushes without it.

Optional developer backends:

- Codex CLI (`npm install -g @openai/codex`, then `codex login`) authenticated in your normal user account when using the `codex` backend
- Claude Code (`curl -fsSL https://claude.ai/install.sh | bash`, then `claude login`) for the `claude-code` backend (Openbase uses your own
  `claude login` directly; `openbase-coder claude login` is a thin wrapper)

## Clone and Run Setup

Clone the workspace repo and run its setup script from the workspace root.
It syncs the sub-repos with `multi`, builds the console from source, and
runs `openbase-coder setup` against your checkout:

```bash
git clone --branch main --single-branch \
  https://github.com/openbase-community/openbase-coder-workspace
cd openbase-coder-workspace
./scripts/setup
```

With no flags, setup runs interactively on a fresh install: numbered pickers
choose the coding backend (`codex`, `claude-code`, or `openbase-cloud`) and
the voice audio provider — Cloud TTS/STT (the recommended default),
bring-your-own-keys (AssemblyAI + Cartesia; setup prompts for the keys), or
local models (not recommended; see [Local-Only Mode](../local-only.md)).

Passing **any** flag disables all prompts, so scripted and AI-agent runs
never block: fresh non-interactive installs require `--backend` and default
the audio provider to `openbase-cloud`. See [setup](../commands/setup.md)
for the full flag list and the `--interactive` override.

Interactive runs offer `openbase-coder login` (browser OAuth), then confirm the device is registered with Openbase Cloud and that the selected private-network transport exposes the local API and LiveKit. Non-interactive runs print the login hint instead. Either way, `./scripts/setup` then builds and launches the developer app (see below), prints "Setup complete" and a short summary of what setup changed on your machine, and finishes with a QR code for the [phone app downloads page](https://openbase.cloud/downloads.html): install the iOS or Android app last.

If a standalone desktop/CLI install, or a different development workspace
install, already exists, the workspace script stops and links to
[Uninstall](../uninstall.md). Uninstall first, then rerun `./scripts/setup`.

Setup never clones or git-updates a workspace itself. When run without
`--workspace-dir` (and no bundled runtime package is present), it discovers
the workspace from the one recorded in `~/.openbase/installation.json`, then
from the checkout behind an editable CLI install; otherwise it errors and asks
you to clone the workspace or use the standalone install.

## Visual developer apps

On macOS, `./scripts/setup` always builds the Electron developer app: it
installs and verifies the Electron runtime, builds the dashboard renderer, and
installs the Openbase launcher in `/Applications`. Setup fails, rather than
reporting success, if any of those steps fails. In a logged-in desktop session, setup then launches the app together with the Swift menu-bar UI automatically. Over SSH, with `OPENBASE_SETUP_NO_LAUNCH=1`, or when a packaged Openbase app already occupies `/Applications`, it only builds them. You can launch either later:

```bash
./scripts/dev-launch --electron  # dashboard/status only; setup is disabled
./scripts/dev-launch --menu-bar  # native Swift Openbase networking status UI
./scripts/dev-launch --all       # both
```

The Electron developer launch requires the `desktop` checkout (part of the
default install set; run `multi sync` to fetch it). Its closed-source netmesh
companion is fetched as a prebuilt signed artifact during the desktop build,
so the open `desktop` sources build without it. It sets an explicit
dashboard-only mode, and Electron also detects the development installation in
`installation.json`; either way, it does not expose the installer bridge.
Never use the Electron onboarding wizard for a development install:
`./scripts/setup` is the only setup authority.

The same launchers are available as VS Code tasks. React/Electron runs from
`tasks.json`; the Swift UI is built with Xcode tools and opened as a menu-bar
app.

## After Setup

Authenticate with Openbase Cloud (required for iOS app pairing and cloud
onboarding):

```bash
openbase-coder login
```

Then verify the install with the
[health check commands](index.md#health-check).

## Start the Server

Setup installs background services that run the server for you. To run it in
the foreground instead — for example while developing:

```bash
openbase-coder server --host 0.0.0.0 --port 7999
```

By default this command:

- Runs Django migrations
- Runs `collectstatic`
- Rebuilds the console in development mode
- Starts Gunicorn + Uvicorn worker(s)

## Next Steps

Continue with the [next steps](index.md#next-steps) on the Getting Started
overview. For the developer install/test workflow, contribution branches, and
service debugging, see the workspace repo's `DEV_RUNBOOK.md`.
