# services install

Generate service files and bootstrap default background services.

## Usage

```bash
openbase-coder services install
```

## What It Does

1. Reads `~/.openbase/installation.json`.
2. Generates service files for the current platform:
   - macOS/Linux: shell wrappers in `~/.openbase/launchd/`
   - macOS: plists in `~/Library/LaunchAgents/`
   - Linux: systemd user units in `~/.config/systemd/user/`
   - Windows: Task Scheduler XML files in `~/.openbase/tasks/`
3. Bootstraps each default service with the platform service manager.
4. Configures the Tailscale Serve routes used by the iOS app:
   - `:18080 -> http://127.0.0.1:7999`
   - `:7880 -> tcp://127.0.0.1:7880`
5. Writes logs to `~/.openbase/logs/`.

Generated macOS/Linux wrappers execute the shared service runner module with
the active Python runtime. Packaged installs prefer the stable Openbase runtime
package; development installs use the Python executable that generated the
service files. The `livekit-server` binary is resolved through the packaged
runtime or `~/.openbase/bin/` when available, with `PATH` and Homebrew
fallbacks.

Optional services, such as the `code-sync` engine, are not installed by
default; they are installed by the feature that needs them (for example
`openbase-coder sync enable`) or can be started explicitly:

```bash
openbase-coder services start code-sync
```
