"""Self-heal for the stale livekit-agent pre-warmed job pool.

Exercises the ``livekit_pool_watchdog`` tick: the ``wait_pc_connection timed
out`` failure-signature watchdog (bounce, escalation, active-call deferral,
rate limit, log truncation) and the proactive idle-recycle branch.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openbase_coder_cli.services import livekit_pool_watchdog as wd

SIGNATURE_LINE = (
    'failed to connect: Connection("wait_pc_connection timed out")\nprocess exiting\n'
)


class _Env:
    def __init__(self, log_path, state_path, bounces, clock, session, running, started):
        self.log_path = log_path
        self.state_path = state_path
        self.bounces = bounces
        self.clock = clock
        self.session = session
        self.running = running
        self.started = started

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
    monkeypatch.setattr(wd.activity, "_ACTIVITY_DIR", tmp_path / "activity")
    monkeypatch.setenv("SUPER_AGENTS_STATE_FILE", str(tmp_path / "super-agents.json"))
    monkeypatch.delenv("OPENBASE_CODER_SERVICE_SUPERVISOR", raising=False)
    monkeypatch.delenv("LIVEKIT_AGENT_IDLE_RECYCLE_SECONDS", raising=False)
    log_path = tmp_path / "livekit-agent.log"
    state_path = tmp_path / "livekit-pool-watchdog.json"
    bounces: list[tuple[str, ...]] = []
    clock = {"now": 1000.0}
    session = {"active": False}
    running = {"ok": True}
    started = {"ts": None}

    monkeypatch.setattr(wd, "_LOG_PATH", log_path)
    monkeypatch.setattr(wd, "_STATE_PATH", state_path)
    monkeypatch.setattr(
        wd, "_execute_bounce", lambda services: bounces.append(services)
    )
    monkeypatch.setattr(wd, "_voice_session_active", lambda: session["active"])
    monkeypatch.setattr(wd, "_agent_service_running", lambda: running["ok"])
    monkeypatch.setattr(wd, "_agent_started_ts", lambda: started["ts"])
    monkeypatch.setattr(wd.time, "time", lambda: clock["now"])

    return _Env(log_path, state_path, bounces, clock, session, running, started)


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


def test_idle_recycle_counts_the_agent_start_as_activity(env):
    """A container restored after sleeping for hours boots a fresh agent while
    the wall clock is far past the recorded baseline (2026-10-07: every tick
    after a Maritime wake recycled the agent)."""
    wd.run_tick()  # baseline at t=1000

    env.advance(10 * 3600)
    env.started["ts"] = env.clock["now"] - 60  # the agent itself is a minute old
    wd.run_tick()
    assert env.bounces == []

    env.advance(wd.IDLE_RECYCLE_SECONDS + 1)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]


def test_failed_idle_bounce_persists_state_and_does_not_refire(env, monkeypatch):
    def explode(services):
        env.bounces.append(services)
        raise RuntimeError("systemctl not found")

    monkeypatch.setattr(wd, "_execute_bounce", explode)
    wd.run_tick()
    env.advance(wd.IDLE_RECYCLE_SECONDS + 1)
    with pytest.raises(RuntimeError):
        wd.run_tick()
    assert env.state()["last_idle_recycle_ts"] == env.clock["now"]

    env.advance(wd.WATCHDOG_TICK_SECONDS)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]  # not a second time


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


def _mark_activity(env, source):
    wd.activity.record_activity(source)
    timestamp = env.clock["now"]
    os.utime(wd.activity._ACTIVITY_DIR / source, (timestamp, timestamp))


@pytest.mark.parametrize("source", ["token", "job", "dispatcher", "thread"])
def test_real_activity_extends_idle_clock(env, source):
    wd.run_tick()
    env.advance(wd.IDLE_RECYCLE_SECONDS - 30)
    if source in {"token", "job"}:
        _mark_activity(env, source)
    else:
        timestamp = datetime.fromtimestamp(env.clock["now"], UTC).isoformat()
        Path(os.environ["SUPER_AGENTS_STATE_FILE"]).write_text(
            json.dumps(
                {
                    "sessions": {
                        source: {
                            "lastStatus": "completed",
                            "lastEventAt": timestamp,
                            "turns": {"turn": {"updatedAt": timestamp}},
                        }
                    },
                }
            )
        )
    env.advance(31)
    wd.run_tick()
    assert env.bounces == []
    env.advance(wd.IDLE_RECYCLE_SECONDS)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]


def test_token_grace_is_a_floor_even_with_short_idle_interval(env, monkeypatch):
    monkeypatch.setenv("LIVEKIT_AGENT_IDLE_RECYCLE_SECONDS", "1")
    wd.run_tick()
    _mark_activity(env, "token")
    env.advance(wd.activity.CALL_JOIN_GRACE_SECONDS - 1)
    wd.run_tick()
    assert env.bounces == []
    env.advance(1)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]


def test_token_grace_also_defers_failure_recovery(env):
    wd.run_tick()
    _mark_activity(env, "token")
    env.append_log(SIGNATURE_LINE)
    env.advance(30)
    wd.run_tick()
    assert env.bounces == []
    assert env.state()["pending"]
    env.advance(wd.activity.CALL_JOIN_GRACE_SECONDS)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]


def test_container_skips_idle_recycling_but_keeps_failure_recovery(env, monkeypatch):
    monkeypatch.setenv("OPENBASE_CODER_SERVICE_SUPERVISOR", "external")
    wd.run_tick()
    env.advance(wd.IDLE_RECYCLE_SECONDS * 100)
    wd.run_tick()
    assert env.bounces == []
    env.append_log(SIGNATURE_LINE)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]


def test_original_boot_chat_call_sequence_does_not_recycle(env):
    wd.run_tick()
    env.advance(120)
    timestamp = datetime.fromtimestamp(env.clock["now"], UTC).isoformat()
    Path(os.environ["SUPER_AGENTS_STATE_FILE"]).write_text(
        json.dumps(
            {
                "sessions": {"dispatcher": {"lastFinishedAt": timestamp}},
            }
        )
    )
    env.advance(44.5 * 60 - 120)
    _mark_activity(env, "token")
    env.advance(30.01)
    wd.run_tick()
    assert env.bounces == []
    assert env.state()["last_idle_recycle_ts"] is None


def test_room_activity_remains_after_room_disappears(env):
    wd.run_tick()
    env.advance(120)
    env.session["active"] = True
    wd.run_tick()
    env.session["active"] = False
    env.advance(wd.IDLE_RECYCLE_SECONDS - 1)
    wd.run_tick()
    assert env.bounces == []
    env.advance(2)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        '{"sessions": []}',
        '{"sessions": {"x": {"lastEventAt": "bad"}}}',
    ],
)
def test_unreadable_activity_cannot_authorize_idle_restart(env, payload):
    wd.run_tick()
    env.advance(wd.IDLE_RECYCLE_SECONDS + 1)
    Path(os.environ["SUPER_AGENTS_STATE_FILE"]).write_text(payload)
    wd.run_tick()
    assert env.bounces == []


def test_call_token_issued_during_room_probe_defers_recycle(env, monkeypatch):
    wd.run_tick()
    env.advance(wd.IDLE_RECYCLE_SECONDS + 1)

    def room_probe():
        # Another process cannot publish under the lock; simulate activity
        # discovered by the final recheck without recursively taking it.
        path = wd.activity._ACTIVITY_DIR / "token"
        path.touch()
        os.utime(path, (env.clock["now"], env.clock["now"]))
        return False

    monkeypatch.setattr(wd, "_voice_session_active", room_probe)
    wd.run_tick()
    assert env.bounces == []


def test_restart_holds_the_token_publication_lock(env, monkeypatch):
    from openbase_coder_cli.file_lock import LOCK_EX, LOCK_NB, flock

    def bounce(services):
        with (wd.activity._ACTIVITY_DIR / "call-start.lock").open("a+b") as handle:
            with pytest.raises(OSError):
                flock(handle, LOCK_EX | LOCK_NB)
        env.bounces.append(services)

    monkeypatch.setattr(wd, "_execute_bounce", bounce)
    wd.run_tick()
    env.advance(wd.IDLE_RECYCLE_SECONDS + 1)
    wd.run_tick()
    assert env.bounces == [("livekit-agent",)]
    # The lock must be released once the restart completes.
    _mark_activity(env, "token")
    assert wd.activity.call_join_pending(env.clock["now"])


def test_pruning_turn_history_does_not_erase_observed_activity(env):
    wd.run_tick()
    env.advance(120)
    path = Path(os.environ["SUPER_AGENTS_STATE_FILE"])
    timestamp = datetime.fromtimestamp(env.clock["now"], UTC).isoformat()
    path.write_text(json.dumps({"sessions": {"thread": {"lastFinishedAt": timestamp}}}))
    wd.run_tick()
    path.write_text('{"sessions": {}}')
    env.advance(wd.IDLE_RECYCLE_SECONDS - 1)
    wd.run_tick()
    assert env.bounces == []
