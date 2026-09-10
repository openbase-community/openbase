# services install

Generate service wrappers and supervisor files, then bootstrap default services.

## Usage

```bash
openbase-coder services install
```

## What It Does

1. Reads `~/.openbase/installation.json`.
2. Generates shell wrappers in `~/.openbase/launchd/`.
3. Generates supervisor files for the host platform:
   - macOS: plists in `~/Library/LaunchAgents/`
   - Linux: systemd user units in `~/.config/systemd/user/`
   - Windows: Task Scheduler XML files in `~/.openbase/tasks/`
4. Bootstraps each default service with the host supervisor (`launchctl`,
   `systemctl --user`, or Task Scheduler).
5. Configures the Tailscale Serve routes used by the iOS app:
   - `:18080 -> http://127.0.0.1:7999`
   - `:7880 -> tcp://127.0.0.1:7880`
6. Writes logs to `~/.openbase/logs/`.

For workspace-managed services, generated wrappers prefer binaries from
`<workspace>/.venv/bin/`, then `<workspace>/.venv/Scripts/`, then
`<workspace>/cli/.venv/bin/`, then `<workspace>/cli/.venv/Scripts/`, then
`<workspace>/agent/.venv/bin/`, then `<workspace>/agent/.venv/Scripts/`
before falling back to `PATH`.
`livekit-server` is resolved from the standalone package when present, then
`~/.openbase/bin/livekit-server`, then `PATH` or `/opt/homebrew/bin/livekit-server`.

Optional services, such as the `code-sync` engine, are not installed by
default; they are installed by the feature that needs them (for example
`openbase-coder sync enable`) or can be started explicitly:

```bash
openbase-coder services start code-sync
```
