`openbase` is the main Openbase Coder runtime repository.

## Service self-healing

LiveKit stale-pool self-healing lives in
`openbase_coder_cli/services/livekit_pool_watchdog.py`, run on the
`sync-workers` tick: it detects the `wait_pc_connection timed out` failure
signature and bounces `livekit-agent` (escalating to `livekit-server` +
`livekit-agent` on recurrence), plus recycles the idle agent so the pool
never goes stale — all with an active-call guard and rate limit. Don't
re-add manual-restart-only advice for the "waiting for agent" WebRTC-timeout
failure; extend that watchdog instead.
