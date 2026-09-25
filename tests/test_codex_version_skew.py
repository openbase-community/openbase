from __future__ import annotations

import json
from pathlib import Path

from openbase_coder_cli.services import codex_version_skew as skew_module
from openbase_coder_cli.services.codex_version_skew import (
    CodexVersionSkew,
    parse_codex_version,
    parse_user_agent_version,
    restart_blockers,
    run_auto_restart_tick,
    super_agents_active_turn_count,
)


def test_parse_codex_version_reads_cli_output() -> None:
    assert parse_codex_version("codex-cli 0.156.1\n") == "0.156.1"
    assert parse_codex_version("") is None


def test_parse_user_agent_version_reads_initialize_handshake() -> None:
    assert (
        parse_user_agent_version("codex-tui/0.155.0 (Mac OS 26.6.2; arm64) unknown")
        == "0.155.0"
    )
    assert parse_user_agent_version("codex-app-server/1.2.3 (x)") == "1.2.3"
    # The originator is whichever client initialized the server first.
    assert parse_user_agent_version("openbase-coder-version-probe/1.2.3 (x)") == "1.2.3"
    assert parse_user_agent_version("no version here") is None
    assert parse_user_agent_version(None) is None


def test_installed_version_caches_per_binary_stat(monkeypatch, tmp_path) -> None:
    binary = tmp_path / "codex"
    binary.write_text("#!/bin/sh\n")
    binary.chmod(0o755)
    calls: list[list[str]] = []

    class FakeResult:
        stdout = "codex-cli 0.156.1\n"
        stderr = ""

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        return FakeResult()

    monkeypatch.setattr(skew_module.subprocess, "run", fake_run)
    monkeypatch.setattr(skew_module, "_installed_cache", None)

    assert skew_module.installed_codex_version(binary) == (str(binary), "0.156.1")
    assert skew_module.installed_codex_version(binary) == (str(binary), "0.156.1")
    assert len(calls) == 1  # unchanged file: no second --version

    binary.write_text("#!/bin/sh\n# changed\n")
    FakeResult.stdout = "codex-cli 0.157.0\n"
    assert skew_module.installed_codex_version(binary) == (str(binary), "0.157.0")
    assert len(calls) == 2


def test_service_version_skew_only_when_versions_differ(monkeypatch) -> None:
    monkeypatch.setattr(skew_module, "service_endpoint", lambda name: f"ep:{name}")
    monkeypatch.setattr(
        skew_module, "running_codex_app_server_version", lambda endpoint: "0.155.0"
    )
    installed = ("/opt/codex", "0.156.1")

    skew = skew_module.service_version_skew("codex-app-server", installed)
    assert skew == CodexVersionSkew(
        service="codex-app-server",
        running_version="0.155.0",
        installed_version="0.156.1",
        installed_path="/opt/codex",
    )
    assert "0.155.0" in skew.message and "0.156.1" in skew.message

    assert (
        skew_module.service_version_skew("codex-app-server", ("/x", "0.155.0")) is None
    )

    # Unreachable server: reported by service status checks, not as skew.
    monkeypatch.setattr(
        skew_module, "running_codex_app_server_version", lambda endpoint: None
    )
    assert skew_module.service_version_skew("codex-app-server", installed) is None


def test_active_turn_count_reads_state_file(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    assert super_agents_active_turn_count(state) == 0  # no file yet

    now = 1_800_000_000.0
    fresh = "2027-01-15T08:00:00+00:00"  # == now
    stale = "2027-01-10T08:00:00+00:00"  # five days earlier

    def session(**fields: object) -> dict[str, object]:
        # Sessions are keyed by threadId; the outer key is only a fallback.
        return {"updatedAt": fresh, **fields}

    def turn(status: str) -> dict[str, str]:
        return {"turnId": "x", "status": status, "startedAt": "x", "updatedAt": "x"}

    state.write_text(
        json.dumps(
            {
                "sessions": {
                    "idle": session(turns={"a": turn("completed")}),
                    "busy": session(activeTurnId="b"),
                    "waiting": session(turns={"c": turn("waiting")}),
                    # Died without a terminal event: still "running" days later.
                    "stale": session(updatedAt=stale, activeTurnId="d"),
                    # Session finished but a turn record never closed.
                    "finished": session(
                        lastStatus="completed", turns={"e": turn("running")}
                    ),
                },
                "routines": {},
            }
        )
    )
    assert super_agents_active_turn_count(state, now=now) == 2

    state.write_text("{not json")
    assert super_agents_active_turn_count(state, now=now) is None


def test_restart_blockers_treat_unknown_as_busy() -> None:
    assert restart_blockers(active_turns=0, voice_active=False) == []
    assert restart_blockers(active_turns=2, voice_active=False) == [
        "2 active agent turn(s)"
    ]
    assert restart_blockers(active_turns=0, voice_active=True) == [
        "voice session in progress"
    ]
    assert restart_blockers(active_turns=None, voice_active=None) == [
        "agent activity unknown",
        "voice session state unknown",
    ]


def _skew(service: str = "codex-app-server") -> CodexVersionSkew:
    return CodexVersionSkew(
        service=service,
        running_version="0.155.0",
        installed_version="0.156.1",
        installed_path="/opt/codex",
    )


def test_auto_restart_tick_restarts_when_idle_once_per_pair(monkeypatch) -> None:
    scheduled: list[tuple[str, ...]] = []
    monkeypatch.setattr(skew_module, "_last_scheduled", {})
    monkeypatch.setattr(skew_module, "collect_codex_version_skews", lambda: [_skew()])
    monkeypatch.setattr(skew_module, "super_agents_active_turn_count", lambda: 0)
    monkeypatch.setattr(skew_module, "voice_session_active", lambda: False)

    from openbase_coder_cli.services import restart as restart_module

    monkeypatch.setattr(
        restart_module,
        "schedule_restart",
        lambda request, **_kwargs: scheduled.append(request.services),
    )

    first = run_auto_restart_tick()
    assert first["restarted"] == ["codex-app-server"]
    assert scheduled == [("codex-app-server",)]

    # Same pair still skewed after the restart: never loop on it.
    second = run_auto_restart_tick()
    assert second["restarted"] == []
    assert second["blockers"] == ["already restarted for this version pair"]
    assert scheduled == [("codex-app-server",)]

    # Skew resolved: the memory clears so a future upgrade restarts again.
    monkeypatch.setattr(skew_module, "collect_codex_version_skews", lambda: [])
    assert run_auto_restart_tick()["skews"] == []
    monkeypatch.setattr(skew_module, "collect_codex_version_skews", lambda: [_skew()])
    assert run_auto_restart_tick()["restarted"] == ["codex-app-server"]


def test_auto_restart_tick_waits_while_busy(monkeypatch) -> None:
    monkeypatch.setattr(skew_module, "_last_scheduled", {})
    monkeypatch.setattr(skew_module, "collect_codex_version_skews", lambda: [_skew()])
    monkeypatch.setattr(skew_module, "super_agents_active_turn_count", lambda: 1)
    monkeypatch.setattr(skew_module, "voice_session_active", lambda: None)

    from openbase_coder_cli.services import restart as restart_module

    def fail(*_args, **_kwargs):
        raise AssertionError("must not restart while busy")

    monkeypatch.setattr(restart_module, "schedule_restart", fail)

    summary = run_auto_restart_tick()
    assert summary["restarted"] == []
    assert summary["blockers"] == [
        "1 active agent turn(s)",
        "voice session state unknown",
    ]


def test_auto_restart_tick_respects_opt_out(monkeypatch) -> None:
    monkeypatch.setenv(skew_module.AUTO_RESTART_ENV, "0")
    monkeypatch.setattr(
        skew_module,
        "collect_codex_version_skews",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe when disabled")),
    )
    assert run_auto_restart_tick() == {"skews": [], "blockers": [], "restarted": []}
