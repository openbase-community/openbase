# Steering Codex TUI Sessions

Openbase's managed Codex app-server and ordinary, newly launched Codex TUIs
share Codex's standard Unix control socket on macOS and Linux:

```text
$CODEX_HOME/app-server-control/app-server-control.sock
```

After Openbase services are ready, launch the TUI normally:

```bash
codex --yolo
```

No wrapper, alias, or `--remote` flag is required. Codex probes the standard
socket and, when the invocation is eligible, connects the TUI to the same
app-server owner used by Super Agents. Openbase can then discover and steer the
TUI's active turn.

This requires Codex 0.151.0 or newer (or a capability-equivalent build with
`app-server --listen unix://` and implicit TUI standard-socket discovery).
Openbase keeps launchd/systemd as the supervisor so its provider, model,
reasoning-effort, and service-tier launch overrides remain unchanged. The
official Codex daemon is still experimental and is not used by this rollout.

Only TUIs launched after the socket is ready attach to the shared owner. An
already-running TUI keeps its embedded owner; restart that TUI to attach it.
Openbase does not adopt or hot-migrate existing sessions.

Codex intentionally keeps an embedded app-server for invocations whose startup
state cannot be replayed by the shared daemon. This includes raw `-c` overrides
(and `--enable`/`--disable` translations), custom profiles or config sources,
strict config, config-loader bypasses, and the hook-trust bypass. Openbase does
not override these exclusions. Users can still opt into an explicit remote
endpoint with Codex's own `--remote` flag when they deliberately need it.

Super Agents treats `notLoaded`, `unknown`, incomplete discovery, and transient
read failures as uncertainty—not proof that a turn is terminal. It will not
implicitly resume a thread or start a replacement turn from those states.
Explicit resume remains available, active turns use `turn/steer`, and confirmed
terminal turns may receive a new follow-up turn.

Windows retains the loopback WebSocket transport until Codex provides an
equivalent local daemon lifecycle or Openbase defines a named-pipe design.
