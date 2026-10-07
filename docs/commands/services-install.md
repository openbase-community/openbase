# services install

Generate service files and bootstrap default background services.

## Usage

```bash
openbase-coder services install
```

## What It Does

1. Reads `~/.openbase/installation.json`.
2. Generates platform service files: launchd plists on macOS, systemd user
   units on Linux, and Task Scheduler XML on Windows.
3. Generates shell wrappers for macOS/Linux services.
4. Bootstraps each default service with the platform service manager.
5. Configures the Tailscale Serve routes used by the iOS app:
   - `:18080 -> http://127.0.0.1:7999`
   - `:7880 -> tcp://127.0.0.1:7880`
6. Writes combined service logs to `~/.openbase/logs/<service>.log`.

Generated services run the Python service runner
(`python -m openbase_coder_cli.services.runners <service>`), which resolves
the service-specific binaries it needs. Development installs can use binaries
from the workspace virtual environments, `~/.openbase/bin`, or explicit
environment overrides; packaged installs prefer the current packaged runtime.
`livekit-server` can also fall back to `/opt/homebrew/bin/livekit-server`.

Optional services, such as the `sync-daemon` (Openbase Sync), are not
installed by default; they are installed by the feature that needs them (for
example `openbase-coder sync-daemon configure`) or can be started explicitly:

```bash
openbase-coder services start sync-daemon
```

Installing services also removes leftover services that no longer exist,
including the retired `code-sync` service of the previous sync.
