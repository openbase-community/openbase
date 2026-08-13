# Manual Setup for the Desktop App

Use this path when you want to finish first-time setup yourself instead of
letting the Electron desktop app run setup commands on your behalf. The
app-driven flow this replaces is described in
[Desktop App](desktop-app.md#install-and-first-run-setup).

You do not need to click the app's setup button for Openbase Coder to work. Run
the commands below in your own terminal, then return to the desktop app after
the health, voice key, and login checks pass.

The desktop app stops showing the setup flow when all of these checks pass:

- The local backend answers `http://127.0.0.1:7999/api/health/`.
- `~/.openbase/.env` contains non-empty `ASSEMBLY_AI_API_KEY` and
  `CARTESIA_API_KEY` values.
- `~/.openbase/auth.json` contains an Openbase access token or refresh token.

## What the App Would Run

The Electron app's setup screen runs the published CLI setup command and streams
the output in the app. The manual setup path is the same underlying operation,
but you run each command yourself from a terminal so you can inspect what will
happen and stop at any point.

Install and open the desktop app first ([Downloads](downloads.md)); its
prerequisite check activates the bundled CLI runtime automatically. Then run
setup yourself:

```bash
openbase-coder setup
```

If your desktop app shows a pinned setup command, prefer the exact command shown
there because it may include app-selected options such as the coding backend.

## Install Prerequisites

Install Tailscale before continuing if you want iPhone-to-Mac voice networking:

```bash
open https://tailscale.com/download/mac
```

The desktop app's bundled runtime package includes Python, Openbase Coder
dependencies, the console build, and LiveKit server. Source-development setup
still needs `uv`, Git, Node, and workspace tooling.

If you want fully local Kokoro/MLX audio, run setup with
`--audio-provider local`; setup installs the optional local-audio packages into
the bundled Python runtime and downloads the required models. This requires an
Apple Silicon Mac and a Python 3.12 runtime — see
[Local-Only Mode](local-only.md).

## Run Setup Yourself

Set the voice keys in your shell before setup if you want the CLI to write them
into a new `~/.openbase/.env` file:

```bash
export ASSEMBLY_AI_API_KEY="<assemblyai-api-key>"
export CARTESIA_API_KEY="<cartesia-api-key>"
```

Then run the pinned desktop-app command or the default standalone setup path:

```bash
openbase-coder setup
```

The setup command writes `~/.openbase/installation.json`, creates
`~/.openbase/.env` if it is missing, uses the bundled console, installs
background services, and configures Tailscale Serve routes.

If `~/.openbase/.env` already existed before setup, the CLI leaves it unchanged.
Add the voice keys manually:

```bash
open -e ~/.openbase/.env
```

Make sure the file contains non-empty values:

```dotenv
ASSEMBLY_AI_API_KEY=<assemblyai-api-key>
CARTESIA_API_KEY=<cartesia-api-key>
```

## Authenticate

Run the CLI login flow from your terminal:

```bash
openbase-coder login
```

The desktop app checks `~/.openbase/auth.json`, so the setup page will continue
to show until login writes an access token or refresh token there.

## Start and Verify Services

Start the managed services:

```bash
openbase-coder services start
```

Then verify the install:

```bash
openbase-coder doctor
openbase-coder services status
curl -fsS http://127.0.0.1:7999/api/health/
```

If Tailscale Serve was not configured during setup, run:

```bash
tailscale serve --bg --http=18080 http://127.0.0.1:7999
tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880
```

## Open the Desktop App

After the health endpoint, voice keys, and login checks pass, reopen or recheck
the Electron app. It should skip the setup flow and load the main Openbase Coder
interface without the app having run any setup commands.
