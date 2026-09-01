# restart

Restart Openbase-managed services.

In the apps: **Settings → Openbase Services** in the
[desktop app](../desktop-app.md) and [console](../console.md) offers the same
restart controls.

## Usage

```bash
openbase-coder restart [OPTIONS]
openbase-coder self-restart [OPTIONS]
```

With no options, this schedules a detached restart of every default
Openbase-managed service:

- all default Openbase background services for the selected backend
- the Openbase Coder API/MCP host through `django-cli`

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
