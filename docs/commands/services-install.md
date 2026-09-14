# services install

Generate service wrappers/definitions and bootstrap default services.

## Usage

```bash
openbase-coder services install
```

## What It Does

1. Reads `~/.openbase/installation.json`.
2. On macOS/Linux, generates shell wrappers in `~/.openbase/launchd/`.
3. Generates launchd plists on macOS, systemd user units on Linux, or Task
   Scheduler XML definitions on Windows.
4. Bootstraps each default service with the platform service manager.
5. Configures the Tailscale Serve routes used by the iOS app:
   - `:18080 -> http://127.0.0.1:7999`
   - `:7880 -> tcp://127.0.0.1:7880`
6. Writes logs to `~/.openbase/logs/`.

Generated wrappers/tasks run
`python -m openbase_coder_cli.services.runners <service>`. Each runner resolves
only the binaries its service needs, preferring the standalone runtime package
or Openbase-managed binaries where available, then service-specific fallbacks
such as `PATH`, `~/.local/bin`, Homebrew paths, or workspace virtualenv paths.

Optional services, such as the `code-sync` engine, are not installed by
default; they are installed by the feature that needs them (for example
`openbase-coder sync enable`) or can be started explicitly:

```bash
openbase-coder services start code-sync
```
