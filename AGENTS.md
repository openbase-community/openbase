`openbase` is the main Openbase Coder runtime repository.

## Code sync (two layers — don't conflate)

Code sync lives in `openbase_coder_cli/code_sync/`. It is **two independent
layers**: **layer 1** is a managed Syncthing file sync of working trees that
categorically excludes `.git`/`.jj`/`.hg` (`code_sync/ignores.py`); **layer 2**
is the reconciler (`code_sync/reconciler.py`, run on the `sync-workers` tick)
that moves git commits and branch pointers between machines over git's own
transport — each machine serves a read-only `git-upload-pack` endpoint at
`/api/sync/git/...` (`openbase_coder_cli_app/git_http.py`), and the reconciler
fetches + fast-forwards when safe. So git state *does* cross machines even
though `.git` never file-syncs. It also creates `refs/heads/synced/<branch>`
mirrors and `refs/openbase-code-sync/backups/*` recovery refs. Canonical
behavior doc: `docs/code-sync.md`; engineer glossary: workspace
`dev-docs/GLOSSARY.md` ("Code sync", "Repo reconciler").

## Service self-healing

LiveKit stale-pool self-healing lives in
`openbase_coder_cli/services/livekit_pool_watchdog.py`, run on the
`sync-workers` tick: it detects the `wait_pc_connection timed out` failure
signature and bounces `livekit-agent` (escalating to `livekit-server` +
`livekit-agent` on recurrence), plus recycles the idle agent so the pool
never goes stale — all with an active-call guard and rate limit. Don't
re-add manual-restart-only advice for the "waiting for agent" WebRTC-timeout
failure; extend that watchdog instead.
