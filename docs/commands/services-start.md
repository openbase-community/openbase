# services start

Start default services or one named service.

## Usage

```bash
openbase-coder services start [NAME]
```

## Examples

```bash
# Start default services
openbase-coder services start

# Start only Django API
openbase-coder services start django-cli

# Start Openbase Sync explicitly (normally started by `openbase-coder sync-daemon pair`)
openbase-coder services start sync-daemon
```

Starting the default service set also configures the Tailscale Serve routes used
by the iOS app:

```bash
tailscale serve --bg --http=18080 http://127.0.0.1:7999
tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880
```
