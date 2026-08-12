# doctor

Validate Openbase local runtime health and security settings.

In the apps: the **Status** page in the [desktop app](../desktop-app.md) and
[console](../console.md) shows the same service health continuously, and the
[iOS app](../ios-tabs.md) shows a warning banner when the runtime these
checks cover is unreachable.

## Usage

```bash
openbase-coder doctor
```

## Checks Performed

- Installation file presence (`installation.json`)
- Standalone package paths for bundled Python, console assets, and LiveKit server
- launchd service install/running state
- Listening ports and bind addresses
- Tailscale Serve routes for the iOS app:
  - `:18080 -> http://127.0.0.1:7999`
  - `:7880 -> tcp://127.0.0.1:7880`
- External Openbase health check through the tailnet `:18080` address
- Required credentials in `.env`
- Detection of known insecure defaults for some keys
- Auth readiness for the selected coding backend:
  - Codex: normal `codex login` plus the Openbase service auth bridge
  - Openbase Cloud: `openbase-coder login`
  - Claude Code: `claude auth login`
- Local audio model readiness when Kokoro or local MLX Whisper is selected

Optional services such as `code-sync` are allowed to be stopped or absent
without causing a doctor failure.

## Required Environment Keys

- `OPENBASE_CODER_CLI_SECRET_KEY`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
