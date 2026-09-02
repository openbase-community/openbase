# services install

Generate service definitions and bootstrap default services.

## Usage

```bash
openbase-coder services install
```

## What It Does

1. Reads `~/.openbase/installation.json`.
2. Generates shell wrappers in `~/.openbase/launchd/` on macOS/Linux.
3. Generates platform service definitions: launchd plists on macOS, systemd
   user units on Linux, or Windows Task Scheduler XML on Windows.
4. Bootstraps each default service with the platform supervisor.
5. Configures the Tailscale Serve routes used by the iOS app:
   - `:18080 -> http://127.0.0.1:7999`
   - `:7880 -> tcp://127.0.0.1:7880`
6. Writes logs to `~/.openbase/logs/`.

For workspace-managed services, generated wrappers prefer binaries from
`<workspace>/.venv/bin/`, then `<workspace>/cli/.venv/bin/`, then
`<workspace>/agent/.venv/bin/`
before falling back to `PATH`.
`livekit-server` is still resolved from `PATH` or `/opt/homebrew/bin/livekit-server`.

Optional services, such as the `code-sync` engine, are not installed by
default; they are installed by the feature that needs them (for example
`openbase-coder sync enable`) or can be started explicitly:

```bash
openbase-coder services start code-sync
```
