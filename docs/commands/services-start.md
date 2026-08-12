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

# Start the code-sync engine explicitly (normally `openbase-coder sync enable`)
openbase-coder services start code-sync
```

Starting the default service set also configures the Tailscale Serve routes used
by the iOS app:

```bash
tailscale serve --bg --http=18080 http://127.0.0.1:7999
tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880
```
