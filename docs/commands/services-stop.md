# services stop

Stop all services or one named service.

## Usage

```bash
openbase-coder services stop [NAME]
```

This unregisters the service from the platform service manager and keeps it
unloaded until restarted. On macOS this uses `launchctl bootout`; on Linux it
disables the systemd user unit, and on Windows it removes the scheduled task.

## Example

```bash
openbase-coder services stop codex-app-server
```
