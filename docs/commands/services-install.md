# services install

Generate wrappers/plists and bootstrap default launchd services.

## Usage

```bash
openbase-coder services install
```

## What It Does

1. Reads `~/.openbase/installation.json`.
2. Generates shell wrappers in `~/.openbase/launchd/`.
3. Generates plists in `~/Library/LaunchAgents/`.
4. Bootstraps each default service with `launchctl`.
5. Configures the Tailscale Serve routes used by the iOS app:
   - `:18080 -> http://127.0.0.1:7999`
   - `:7880 -> tcp://127.0.0.1:7880`
6. Writes logs to `~/.openbase/logs/`.

For workspace-managed services, generated wrappers prefer binaries from
`<workspace>/.venv/bin/`, then `<workspace>/cli/.venv/bin/`, then
`<workspace>/agent/.venv/bin/`
before falling back to `PATH`.
`livekit-server` is still resolved from `PATH` or `/opt/homebrew/bin/livekit-server`.

Optional services, such as the `sync-daemon` (Openbase Sync), are not
installed by default; they are installed by the feature that needs them (for
example pairing on the console Sync page or `openbase-coder sync-daemon pair`)
or can be started explicitly:

```bash
openbase-coder services start sync-daemon
```

Installing services also removes leftover services that no longer exist,
including the retired `code-sync` service of the previous sync.
