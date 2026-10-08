`openbase` is the main Openbase Coder runtime repository.

## Code sync (Openbase Sync)

File and git sync between a user's computers is done by the closed-source
Openbase Sync daemon (`openbase-syncd`, the `sync-daemon` service). This repo
only talks to it over its unix control socket: the client is
`openbase_coder_cli/sync_daemon.py`, the commands are `openbase-coder
sync-daemon ...` and `openbase-coder sync status|conflicts|resolve|
migrate-from-syncthing`, and the API routes are thin proxies
(`openbase_coder_cli_app/sync_daemon_api.py`). Never implement sync logic or
add daemon source here. The previous Syncthing-based `code_sync` package was
removed; `sync migrate-from-syncthing` moves old machines over. Canonical
behavior doc: `docs/code-sync.md`; engineer glossary: workspace
`dev-docs/GLOSSARY.md` ("Code sync").

## Service self-healing

LiveKit stale-pool self-healing lives in
`openbase_coder_cli/services/livekit_pool_watchdog.py`, run on the
`sync-workers` tick: it detects the `wait_pc_connection timed out` failure
signature and bounces `livekit-agent` (escalating to `livekit-server` +
`livekit-agent` on recurrence), plus recycles the idle agent so the pool
never goes stale — all with an active-call guard and rate limit. Don't
re-add manual-restart-only advice for the "waiting for agent" WebRTC-timeout
failure; extend that watchdog instead.

Codex version-skew self-healing lives in
`openbase_coder_cli/services/codex_version_skew.py`, also on the
`sync-workers` tick: a `codex-app-server` (or the dispatcher instance) that
reports an older version over its `initialize` handshake than the codex
binary the service resolver would exec today is restarted once nothing is
in flight: no recent Super Agents turn, no voice call, and no thread loaded
in that app-server (interactive `codex` TUIs attach to it) that is active or
was updated within `OPENBASE_CODEX_RECENT_THREAD_SECONDS` (default 600) —
probed via `thread/loaded/list` + `thread/read` (once per version pair,
never in a loop). The same skew feeds the console health banner as
`service-restart-needed:<service>` with a one-click restart, and
`services status` prints it. Set `OPENBASE_CODEX_AUTO_RESTART=0` in
`~/.openbase/.env` to keep the banner but disable the automatic restart.
Only a skew a restart resolves (`CodexVersionSkew.restart_resolves`) is
restarted: a server *newer* than the installed CLI, or one served by Codex's
own managed daemon (the standard control socket is a symlink, see
`codex_control_plane.endpoint_is_shared_codex_daemon`), is advisory — a
`codex-cli-outdated:<service>` banner warning and a once-per-pair log line
telling the user to upgrade the Codex CLI. Openbase never restarts the shared
daemon, and the `codex-app-server` runner idles instead of binding while that
daemon owns the socket (`codex_control_plane.idle_while_shared_codex_daemon`).
