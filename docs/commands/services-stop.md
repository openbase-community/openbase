# services stop

Stop all services or one named service.

## Usage

```bash
openbase-coder services stop [NAME]
```

This stops the platform-managed job and keeps it unloaded or disabled until
restarted. On macOS it uses `launchctl bootout`; on Linux it disables and stops
the systemd user unit; on Windows it ends and deletes the scheduled task.

## Example

```bash
openbase-coder services stop codex-app-server
```
