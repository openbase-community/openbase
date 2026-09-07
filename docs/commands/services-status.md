# services status

Show install/running state for all managed services.

## Usage

```bash
openbase-coder services status
```

Output includes whether each service is:

- `not installed`
- `running (pid ...)`
- `loaded (not running, last exit: ...)`
- `optional (not installed)`
- `optional (not running, last exit: ...)`
- `not used (... backend)`

For the `codex-app-server` service, the command also verifies initialize
readiness after the service reports a PID.

The command also verifies the tailnet routes that the apps use. Stock
Tailscale installs configure these routes with:

```bash
tailscale serve --bg --http=18080 http://127.0.0.1:7999
tailscale serve --bg --tcp=7880 tcp://127.0.0.1:7880
```

`services status` exits non-zero if any required managed service is unhealthy,
either route is missing, or the external Openbase health check at the tailnet
`:18080` address fails. Optional services are reported but do not make the
command fail when stopped.
