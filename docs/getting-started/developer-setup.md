# Developer Setup

Installing from a workspace checkout is a fully supported install path. Use
it when you want to develop Openbase Coder itself, run the runtime from
source, or set up a machine without the desktop app (for example a headless
Linux box you administer over SSH).

## Prerequisites

In addition to the shared [prerequisites](index.md#prerequisites),
development installs need:

- Git
- [`uv`](https://docs.astral.sh/uv/)
- Node and npm (or pnpm) for building the console from source

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
local models (not recommended; see [Local-Only Mode](../local-only.md)). Pass
`--backend` and `--audio-provider` to skip the pickers — see
[setup](../commands/setup.md).

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
