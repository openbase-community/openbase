# restart

Restart Openbase-managed services.

In a developer install, a restart that includes `livekit-server` first checks the installed engine against the source checkout's version pin and downloads the matching engine if needed. This includes a full restart and `--service livekit-server`; restarting only `livekit-agent` or an unrelated service does not update the engine. Preparation finishes before the restart is scheduled. If the download fails or reports the wrong version, the command fails and leaves services running so you can resolve the error and retry. Packaged installs continue to use their bundled engine.

In the apps: **Settings → Openbase Services** in the
[desktop app](../desktop-app.md) and [console](../console.md) offers the same
restart controls.

## Usage

```bash
openbase-coder restart [OPTIONS]
openbase-coder self-restart [OPTIONS]
```

With no options, this schedules a detached restart of the default services plus optional daemons already enabled on this installation, including code sync, the Cloud heartbeat, and Openbase Direct. It does not enable unused optional features or rerun completed one-shot authentication/provisioning jobs. The Openbase Coder API/MCP host restarts through `django-cli`.

In a developer install, a restart that includes `openbase-tunneld` rebuilds and installs Openbase Direct from the configured checkout before scheduling the restart. A failed build leaves the running services alone. The new binary receives a build stamp so runtime freshness can verify it after startup.

The native Openbase VPN app, companion, and helper capture developer build provenance at startup. Rebuild their signed bundles, replace the helper through the existing companion registration flow, and relaunch the native apps after native source changes. A managed-service restart does not restart these separate native components. An older or unstamped native build remains unverified; rebuilding a bundle alone cannot clear a still-running old process. Screen-sharing companions and mobile clients remain outside source-verification coverage.

Dispatcher context is preserved by default. Use `--recreate-dispatcher` when
you need a new dispatcher thread; a normal restart intentionally keeps the
existing dispatcher route state.

The Super Agents MCP stdio process is owned by the client that spawned it, such as Codex.
`openbase-coder restart` does not kill or restart that process.

## Options

| Option | Default | Description |
|---|---|---|
| `--service NAME` | all services | Restart exactly one Openbase-managed service |
| `--delay FLOAT` | `8.0` | Seconds to wait before restarting |
| `--recreate-dispatcher` | off | Clear dispatcher state and recreate it during restart |

`openbase-coder self-restart` is an alias for a full Openbase-managed service
restart. It supports `--delay` and `--recreate-dispatcher`, but does not accept
`--service`.

## Examples

```bash
openbase-coder restart
openbase-coder restart --service livekit-agent
openbase-coder restart --recreate-dispatcher
openbase-coder self-restart --recreate-dispatcher
```
