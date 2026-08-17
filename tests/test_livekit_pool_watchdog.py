"""Self-heal for the stale livekit-agent pre-warmed job pool.

Exercises the ``livekit_pool_watchdog`` tick: the ``wait_pc_connection timed
out`` failure-signature watchdog (bounce, escalation, active-call deferral,
rate limit, log truncation) and the proactive idle-recycle branch.
"""

from __future__ import annotations

import json

import pytest

from openbase_coder_cli.services import livekit_pool_watchdog as wd

SIGNATURE_LINE = (
    'failed to connect: Connection("wait_pc_connection timed out")\nprocess exiting\n'
)


class _Env:
    def __init__(self, log_path, state_path, bounces, clock, session, running):
        self.log_path = log_path
        self.state_path = state_path
        self.bounces = bounces
        self.clock = clock
        self.session = session
        self.running = running

    def append_log(self, text: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def write_log(self, text: str) -> None:
        self.log_path.write_text(text, encoding="utf-8")

    def advance(self, seconds: float) -> None:
        self.clock["now"] += seconds

    def state(self) -> dict:
        return json.loads(self.state_path.read_text(encoding="utf-8"))


@pytest.fixture
def env(monkeypatch, tmp_path):
    log_path = tmp_path / "livekit-agent.log"
    state_path = tmp_path / "livekit-pool-watchdog.json"
    bounces: list[tuple[str, ...]] = []
    clock = {"now": 1000.0}
    session = {"active": False}
    running = {"ok": True}

    monkeypatch.setattr(wd, "_LOG_PATH", log_path)
    monkeypatch.setattr(wd, "_STATE_PATH", state_path)
    monkeypatch.setattr(
        wd, "_execute_bounce", lambda services: bounces.append(services)
    )
    monkeypatch.setattr(wd, "_voice_session_active", lambda: session["active"])
    monkeypatch.setattr(wd, "_agent_service_running", lambda: running["ok"])
    monkeypatch.setattr(wd.time, "time", lambda: clock["now"])

    return _Env(log_path, state_path, bounces, clock, session, running)


def test_first_run_seeks_to_eof_and_ignores_historical_signature(env):
    # A pre-existing failure from before the watchdog ever ran must not fire.
    env.write_log(SIGNATURE_LINE)

    wd.run_tick()
    assert env.bounces == []
    assert env.state()["initialized"] is True

    # A second tick with no new content still does nothing.
    wd.run_tick()
    assert env.bounces == []


def test_new_signature_bounces_agent_only(env):
    wd.run_tick()  # initialize at EOF
    env.append_log(SIGNATURE_LINE)

    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]


def test_recurrence_within_window_escalates_to_server_and_agent(env):
    wd.run_tick()
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]

    env.advance(60)  # still within the 15-min escalation window
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()
    assert env.bounces == [
        ("livekit-agent",),
        ("livekit-server", "livekit-agent"),
    ]


def test_recurrence_after_window_does_not_escalate(env):
    wd.run_tick()
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()

    env.advance(wd.ESCALATION_WINDOW_SECONDS + 1)
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",), ("livekit-agent",)]


def test_active_call_defers_bounce_until_session_ends(env):
    wd.run_tick()
    env.session["active"] = True
    env.append_log(SIGNATURE_LINE)

    wd.run_tick()
    assert env.bounces == []
    assert env.state()["pending"] is not None

    # The signature lines were already consumed; the pending flag carries the
    # intent so the next tick bounces once the call ends.
    env.session["active"] = False
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]
    assert env.state()["pending"] is None


def test_pending_bounce_expires_after_ttl(env):
    wd.run_tick()
    env.session["active"] = True
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()
    assert env.state()["pending"] is not None

    # Session never ends within the TTL: the stale intent is dropped.
    env.session["active"] = False
    env.advance(wd.PENDING_TTL_SECONDS + 1)
    wd.run_tick()
    assert env.bounces == []
    assert env.state()["pending"] is None


def test_rate_limit_blocks_a_fourth_bounce_in_window(env):
    wd.run_tick()
    for _ in range(wd.RATE_LIMIT_MAX_BOUNCES):
        env.advance(30)
        env.append_log(SIGNATURE_LINE)
        wd.run_tick()
    assert len(env.bounces) == wd.RATE_LIMIT_MAX_BOUNCES

    env.advance(30)
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()
    assert len(env.bounces) == wd.RATE_LIMIT_MAX_BOUNCES  # blocked

    # Once the window rolls past, self-heal resumes.
    env.advance(wd.RATE_LIMIT_WINDOW_SECONDS + 1)
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()
    assert len(env.bounces) == wd.RATE_LIMIT_MAX_BOUNCES + 1


def test_log_truncation_resets_offset_without_refiring(env):
    wd.run_tick()
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]
    offset_before = env.state()["log_offset"]
    assert offset_before > 0

    # Service restart truncates the log to its last lines — the file shrinks
    # and may still contain the signature in its tail. We must not re-fire.
    env.write_log("wait_pc_connection timed out\n")
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]
    assert env.state()["log_offset"] == env.log_path.stat().st_size


def test_idle_recycle_fires_only_after_threshold_from_baseline(env):
    wd.run_tick()  # sets the idle baseline

    env.advance(wd.IDLE_RECYCLE_SECONDS - 1)
    wd.run_tick()
    assert env.bounces == []  # not yet idle long enough

    env.advance(2)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]
    assert env.state()["last_idle_recycle_ts"] is not None
    # An idle recycle does not advance the failure escalation ladder.
    assert env.state()["last_failure_bounce_ts"] is None


def test_idle_recycle_disabled_when_env_non_positive(env, monkeypatch):
    monkeypatch.setenv("LIVEKIT_AGENT_IDLE_RECYCLE_SECONDS", "0")
    wd.run_tick()

    env.advance(wd.IDLE_RECYCLE_SECONDS * 10)
    wd.run_tick()
    assert env.bounces == []


def test_idle_recycle_skipped_during_active_call(env):
    wd.run_tick()
    env.session["active"] = True
    env.advance(wd.IDLE_RECYCLE_SECONDS + 1)
    wd.run_tick()
    assert env.bounces == []


def test_service_not_running_is_a_noop(env):
    env.running["ok"] = False
    env.write_log(SIGNATURE_LINE)

    wd.run_tick()
    assert env.bounces == []
    assert not env.state_path.exists()  # no state written on skip


def test_corrupt_state_file_is_tolerated(env):
    env.state_path.write_text("{not valid json", encoding="utf-8")
    env.write_log(SIGNATURE_LINE)

    # Resets to fresh state and re-initializes at EOF rather than crashing.
    wd.run_tick()
    assert env.bounces == []
    assert env.state()["initialized"] is True


def test_sync_job_is_registered_in_build_jobs():
    from openbase_coder_cli.cli.sync_workers import build_jobs

    jobs = {job.name: job for job in build_jobs()}
    assert "livekit_pool_watchdog" in jobs
    assert jobs["livekit_pool_watchdog"].tick is not None
