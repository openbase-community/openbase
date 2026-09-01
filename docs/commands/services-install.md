# services install

Generate service files and bootstrap default background services.

## Usage

```bash
openbase-coder services install
```

## What It Does

1. Reads `~/.openbase/installation.json`.
2. On macOS and Linux, generates shell wrappers in `~/.openbase/launchd/`.
3. Generates supervisor files for the host: launchd plists in
   `~/Library/LaunchAgents/` on macOS, systemd user units in
   `~/.config/systemd/user/` on Linux, or Task Scheduler XML in
   `~/.openbase/tasks/` on Windows.
4. Bootstraps each default service with the host supervisor.
5. Configures the Tailscale Serve routes used by the iOS app:
   - `:18080 -> http://127.0.0.1:7999`
   - `:7880 -> tcp://127.0.0.1:7880`
6. Writes logs to `~/.openbase/logs/`.

For workspace-managed services, generated wrappers prefer binaries from
`<workspace>/.venv/bin/`, then `<workspace>/cli/.venv/bin/`, then
`<workspace>/agent/.venv/bin/`
before falling back to `PATH`.
`livekit-server` is resolved from the standalone package when present,
`~/.openbase/bin/`, `PATH`, or platform-specific fallback locations such as
Homebrew.

Optional services, such as the `code-sync` engine, are not installed by
default; they are installed by the feature that needs them (for example
`openbase-coder sync enable`) or can be started explicitly:

```bash
openbase-coder services start code-sync
```
