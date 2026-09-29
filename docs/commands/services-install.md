# services install

Generate service files and bootstrap default Openbase services.

## Usage

```bash
openbase-coder services install
```

## What It Does

1. Reads `~/.openbase/installation.json`.
2. Generates service runner files for the host platform.
3. On macOS, writes shell wrappers in `~/.openbase/launchd/` and plists in
   `~/Library/LaunchAgents/`; on Linux, writes systemd user units; on Windows,
   writes Task Scheduler XML.
4. Starts each default service through the host service manager.
5. Configures the Tailscale Serve routes used by the iOS app:
   - `:18080 -> http://127.0.0.1:7999`
   - `:7880 -> tcp://127.0.0.1:7880`
6. Writes logs to `~/.openbase/logs/`.

For workspace-managed services, generated wrappers prefer binaries from
`<workspace>/.venv/bin/`, then `<workspace>/cli/.venv/bin/`, then
`<workspace>/agent/.venv/bin/`
before falling back to `PATH`.
`livekit-server` prefers the packaged binary or `~/.openbase/bin/livekit-server`,
then falls back to `PATH` and `/opt/homebrew/bin/livekit-server`.

Optional services, such as the `code-sync` engine, are not installed by
default; they are installed by the feature that needs them (for example
`openbase-coder sync enable`) or can be started explicitly:

```bash
openbase-coder services start code-sync
```
