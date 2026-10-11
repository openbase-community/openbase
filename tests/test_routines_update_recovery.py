"""Exercise the runner's real scheduling loop with a deterministic clock."""

from importlib import import_module
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from openbase_coder_cli import runtime, self_update, skills_autolink


@pytest.mark.parametrize("killed_worker", [False, True])
def test_runner_retries_failed_feed_or_signal_killed_worker(monkeypatch, killed_worker):
    routines = import_module("openbase_coder_cli.cli.routines")
    now = [0]
    checks, spawns = [], []
    monkeypatch.setattr(routines.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(routines, "_run_client", lambda _: {"count": 0})
    monkeypatch.setattr(
        skills_autolink, "sync_auto_linked_skills", lambda: {"enabled": False}
    )
    monkeypatch.setattr(runtime, "is_standalone_runtime", lambda: True)
    monkeypatch.setattr(self_update, "auto_update_enabled", lambda: True)

    def check():
        checks.append(now[0])
        if not killed_worker and len(checks) < 3:
            raise self_update.RetryableUpdateError("offline")
        return self_update.UpdateCheck("1", "2", "stable", True, False)

    def spawn(**_):
        spawns.append(now[0])
        return SimpleNamespace(
            poll=lambda: -9 if len(spawns) == 1 and killed_worker else None
        )

    def sleep(seconds):
        now[0] += seconds
        if now[0] >= 300:
            raise KeyboardInterrupt

    monkeypatch.setattr(self_update, "check_for_update", check)
    monkeypatch.setattr(self_update, "spawn_detached_self_update", spawn)
    monkeypatch.setattr(routines.time, "sleep", sleep)
    result = CliRunner().invoke(routines.routines, ["run-loop", "--interval", "60"])
    assert result.exit_code == 1
    assert checks == ([0, 120] if killed_worker else [0, 60, 180])
    assert spawns == ([0, 120] if killed_worker else [180])


def test_runner_notices_pending_activation_before_six_hour_feed_check(monkeypatch):
    routines = import_module("openbase_coder_cli.cli.routines")
    now = [0]
    spawns = []
    monkeypatch.setattr(routines.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(routines, "_run_client", lambda _: {"count": 0})
    monkeypatch.setattr(
        skills_autolink, "sync_auto_linked_skills", lambda: {"enabled": False}
    )
    monkeypatch.setattr(runtime, "is_standalone_runtime", lambda: True)
    monkeypatch.setattr(self_update, "auto_update_enabled", lambda: True)
    monkeypatch.setattr(self_update, "activation_pending", lambda: now[0] >= 60)
    monkeypatch.setattr(
        self_update,
        "check_for_update",
        lambda: self_update.UpdateCheck("2", "2", "stable", now[0] >= 60, False),
    )

    def spawn(**_):
        spawns.append(now[0])
        return SimpleNamespace(
            poll=lambda: 1
        )  # Incomplete recovery retains its journal.

    def sleep(seconds):
        now[0] += seconds
        if now[0] >= 300:
            raise KeyboardInterrupt

    monkeypatch.setattr(self_update, "spawn_detached_self_update", spawn)
    monkeypatch.setattr(routines.time, "sleep", sleep)
    result = CliRunner().invoke(routines.routines, ["run-loop", "--interval", "60"])
    assert result.exit_code == 1
    assert spawns == [120, 240]
