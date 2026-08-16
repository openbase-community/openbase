# Developer Setup

Installing from a workspace checkout is a fully supported install path, with
an interactive terminal flow: run `./scripts/setup` with no flags and it
picks your coding backend and voice audio provider, walks you through
Openbase Cloud login, and verifies the install. Use it when you want to
develop Openbase Coder itself, run the runtime from source, or set up a
machine without the desktop app (for example a headless Linux box you
administer over SSH). (Just want the product on a Mac? See
[Mac App Download](mac-app.md). On Windows, see
[Run in Docker](../docker.md).)

## Prerequisites

In addition to the shared [prerequisites](index.md#prerequisites),
development installs need:

- Git
- [`uv`](https://docs.astral.sh/uv/)
- Node 20+ and pnpm for building the console from source (the setup script
  checks both and fails fast with install instructions)

Optional developer backends:

- Codex CLI authenticated in your normal user account when using the `codex` backend
- Claude Code login for the `claude-code` backend (on macOS, setup bridges
  your normal Claude Code login into Openbase's managed config automatically
  when it can; `openbase-coder claude login` is the fallback)

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

Interactive runs finish by offering `openbase-coder login` (browser OAuth),
then confirm the device is registered with Openbase Cloud and that Tailscale
Serve is exposing the local API and LiveKit, and print a QR code for the
[phone app downloads page](https://openbase.cloud/downloads.html).
Non-interactive runs end with the login hint instead, exactly as before.

If a standalone desktop/CLI install, or a different development workspace
install, already exists, the workspace script stops and links to
[Uninstall](../uninstall.md). Uninstall first, then rerun `./scripts/setup`.

Setup never clones or git-updates a workspace itself. When run without
`--workspace-dir` (and no bundled runtime package is present), it discovers
the workspace from the one recorded in `~/.openbase/installation.json`, then
from the checkout behind an editable CLI install; otherwise it errors and asks
you to clone the workspace or use the standalone install.

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
