# server

Run the local Openbase API and WebSocket server.

## Usage

```bash
openbase-coder server [OPTIONS]
```

## Options

| Option | Default | Description |
|---|---|---|
| `--host TEXT` | `127.0.0.1` | Bind host |
| `--port INTEGER` | `7999` | Bind port |
| `--workers INTEGER` | `1` | Worker process count — leave at `1` (see below) |
| `--reload` | `false` | Enable auto-reload |
| `--skip-migrations` | `false` | Skip Django migrations |
| `--skip-collectstatic` | `false` | Skip static collection |

## Startup Sequence

By default `server` does the following:

1. Sets Django environment.
2. Creates data directory (`~/.openbase` by default).
3. Runs migrations.
4. Runs `collectstatic` into `~/.openbase/staticfiles`.
5. Builds the console bundle.
6. Starts Uvicorn (or Gunicorn with Uvicorn workers when `--workers` is
   greater than 1).

> **Warning:** Leave `--workers` at `1`. WebSocket updates are broadcast
> through an in-process channel layer that does not span worker processes,
> so with multiple workers thread streaming and app-control messages are
> silently dropped for connections on other workers.

## Example

```bash
openbase-coder server --host 0.0.0.0 --port 7999
```

## Related Endpoints

- REST API: `http://<host>:<port>/api/...`
- WebSockets: `ws://<host>:<port>/ws/threads/...`
- Console SPA: `http://<host>:<port>/`
