# services

Manage Openbase launchd background services.

In the apps: the [desktop app](../desktop-app.md) and
[console](../console.md) show the same services on their **Status** page and
offer start/stop/restart under **Settings → Openbase Services**; these
services are also what the [iOS app](../ios-tabs.md) connects to.

## Usage

```bash
openbase-coder services COMMAND [ARGS]
```

## Subcommands

| Subcommand | Description |
|---|---|
| [`install`](services-install.md) | Generate wrappers/plists and load default services |
| `up` | Alias for `install`; also configures Tailscale Serve routes |
| [`start`](services-start.md) | Start default services or one named service |
| [`stop`](services-stop.md) | Stop all or one service |
| [`status`](services-status.md) | Show service state summary |
| [`logs`](services-logs.md) | Tail one service's logs |
| [`regenerate`](services-regenerate.md) | Rebuild wrappers/plists from `installation.json` |

## Managed Service Names

- `livekit-server` (port `7880`)
- `codex-app-server` (standard Unix control socket on macOS/Linux; legacy port
  `4500` only for explicit compatibility deployments)
- `sync-workers` (cross-device thread snapshot sync, Claude desktop app
  index sync, code-sync reconcile, and other periodic jobs in one process;
  state-dependent jobs no-op until their feature is enabled)
- `openbase-routines`
- `livekit-agent`
- `django-cli` (port `7999`)
- `code-sync` (optional; installed when code sync is enabled)

## Examples

```bash
openbase-coder services install
openbase-coder services up
openbase-coder services status
openbase-coder restart --service django-cli
openbase-coder services logs codex-app-server
```
