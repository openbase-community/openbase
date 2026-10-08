from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from openbase_coder_cli.services import codex_version_skew as skew_module
from openbase_coder_cli.services.codex_version_skew import (
    CodexVersionSkew,
    attached_sessions_in_use,
    parse_codex_version,
    parse_user_agent_version,
    restart_blockers,
    run_auto_restart_tick,
    super_agents_active_turn_count,
    thread_in_use,
)


@pytest.fixture(autouse=True)
def isolate_managed_binary_repair(monkeypatch):
    monkeypatch.setattr(skew_module, "repair_unusable_managed_codex", lambda: False)


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
        returncode = 0
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


def test_installed_version_invalidates_atomic_replacement_with_identical_stat(
    monkeypatch, tmp_path
):
    binary = tmp_path / "codex"
    binary.write_text("first")
    candidate = tmp_path / "candidate"
    candidate.write_text("other")
    stamp = binary.stat()
    os.utime(candidate, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    results = iter(["0.160.1", "0.161.0"])
    monkeypatch.setattr(skew_module, "_installed_cache", None)
    monkeypatch.setattr(
        skew_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout="codex-cli " + next(results), stderr=""
        ),
    )
    assert skew_module.installed_codex_version(binary)[1] == "0.160.1"
    candidate.replace(binary)
    assert binary.stat().st_size == stamp.st_size
    assert binary.stat().st_mtime_ns == stamp.st_mtime_ns
    assert skew_module.installed_codex_version(binary)[1] == "0.161.0"


def test_installed_version_reprobes_in_place_rewrite_preserving_mtime(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_text("first")
    stamp = binary.stat()
    results = iter([
        SimpleNamespace(returncode=0, stdout="codex-cli 0.161.0", stderr=""),
        SimpleNamespace(returncode=-9, stdout="", stderr=""),
    ])
    monkeypatch.setattr(skew_module, "_installed_cache", None)
    monkeypatch.setattr(skew_module.subprocess, "run", lambda *_args, **_kwargs: next(results))
    assert skew_module.installed_codex_version(binary)[1] == "0.161.0"
    binary.write_text("other")
    os.utime(binary, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert binary.stat().st_ino == stamp.st_ino
    assert binary.stat().st_size == stamp.st_size
    assert binary.stat().st_mtime_ns == stamp.st_mtime_ns
    assert skew_module.installed_codex_version(binary) is None


def test_installed_version_retries_failed_probe_without_file_change(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.touch()
    results = iter([
        SimpleNamespace(returncode=-9, stdout="codex-cli 0.161.0", stderr=""),
        SimpleNamespace(returncode=0, stdout="codex-cli 0.161.0", stderr=""),
    ])
    monkeypatch.setattr(skew_module, "_installed_cache", None)
    monkeypatch.setattr(skew_module.subprocess, "run", lambda *_args, **_kwargs: next(results))
    assert skew_module.installed_codex_version(binary) is None
    assert skew_module.installed_codex_version(binary)[1] == "0.161.0"


def test_auto_restart_repairs_before_detecting_skew(monkeypatch):
    events = []
    monkeypatch.setattr(skew_module, "auto_restart_enabled", lambda: True)
    monkeypatch.setattr(skew_module, "repair_unusable_managed_codex", lambda: events.append("repair"))
    monkeypatch.setattr(skew_module, "collect_codex_version_skews", lambda: events.append("detect") or [])
    assert run_auto_restart_tick()["restarted"] == []
    assert events == ["repair", "detect"]


def test_auto_restart_opt_out_also_disables_binary_repair(monkeypatch):
    monkeypatch.setattr(skew_module, "auto_restart_enabled", lambda: False)
    monkeypatch.setattr(skew_module, "repair_unusable_managed_codex", lambda: pytest.fail("repair disabled"))
    assert run_auto_restart_tick()["restarted"] == []


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
    assert restart_blockers(active_turns=0, voice_active=False, sessions_in_use=3) == [
        "3 Codex session(s) in use"
    ]
    assert restart_blockers(
        active_turns=0, voice_active=False, sessions_in_use=None
    ) == ["Codex session state unknown"]


def test_thread_in_use_distinguishes_mid_run_from_idle_tabs() -> None:
    now = 1_800_000_000.0
    window = 600.0

    def thread(status: object, updated_at: object) -> dict[str, object]:
        return {"id": "t", "status": status, "updatedAt": updated_at}

    # A running turn (or one waiting on approval) is in use regardless of age.
    active = {"type": "active", "activeFlags": ["waitingOnApproval"]}
    assert thread_in_use(thread(active, now - 3600), now=now, recent_seconds=window)
    # Idle but touched moments ago: the user is reading or typing.
    assert thread_in_use(
        thread({"type": "idle"}, now - 30), now=now, recent_seconds=window
    )
    # Millisecond timestamps are normalised.
    assert thread_in_use(
        thread({"type": "idle"}, (now - 30) * 1000), now=now, recent_seconds=window
    )
    # A tab left open for an hour is not in use.
    assert not thread_in_use(
        thread({"type": "idle"}, now - 3600), now=now, recent_seconds=window
    )
    # Threads the server is not actually running never block.
    assert not thread_in_use(
        thread({"type": "notLoaded"}, now), now=now, recent_seconds=window
    )
    assert not thread_in_use(
        thread({"type": "systemError"}, now), now=now, recent_seconds=window
    )
    # Unknown status or an unreadable thread: treat as busy.
    assert thread_in_use(
        thread({"type": "future"}, now - 3600), now=now, recent_seconds=window
    )
    assert thread_in_use(thread(None, now - 3600), now=now, recent_seconds=window)
    assert thread_in_use(None, now=now, recent_seconds=window)


class _FakeAppServer:
    """Answers initialize, thread/loaded/list and thread/read like the app-server."""

    def __init__(
        self, loaded: list[str], threads: dict[str, dict], *, page_size: int = 2
    ):
        self.loaded = loaded
        self.threads = threads
        self.page_size = page_size
        self.methods: list[str] = []
        self._outbox: list[dict] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        request = json.loads(raw)
        method, params, rid = request["method"], request["params"], request["id"]
        self.methods.append(method)
        # Interleave a notification to prove unrelated messages are skipped.
        self._outbox.append({"method": "thread/status/changed", "params": {}})
        if method == "initialize":
            self._outbox.append(
                {"id": rid, "result": {"userAgent": "codex-app-server/0.158.0 (x)"}}
            )
        elif method == "thread/loaded/list":
            start = int(params.get("cursor") or 0)
            end = start + self.page_size
            page = self.loaded[start:end]
            self._outbox.append(
                {
                    "id": rid,
                    "result": {
                        "data": page,
                        "nextCursor": str(end) if end < len(self.loaded) else None,
                    },
                }
            )
        elif method == "thread/read":
            thread = self.threads.get(params["threadId"])
            if thread is None:
                self._outbox.append(
                    {
                        "id": rid,
                        "error": {"code": -32600, "message": "thread not loaded"},
                    }
                )
            else:
                self._outbox.append({"id": rid, "result": {"thread": thread}})
        else:
            raise AssertionError(f"unexpected method {method}")

    async def recv(self) -> str:
        return json.dumps(self._outbox.pop(0))

    async def close(self) -> None:
        self.closed = True


def test_attached_sessions_in_use_counts_loaded_threads_via_app_server(
    monkeypatch,
) -> None:
    now = 1_800_000_000.0
    servers: dict[str, _FakeAppServer] = {
        "ep:codex-app-server": _FakeAppServer(
            loaded=["mid-run", "typing", "stale-tab", "vanished"],
            threads={
                "mid-run": {"status": {"type": "active"}, "updatedAt": now - 5},
                "typing": {"status": {"type": "idle"}, "updatedAt": now - 20},
                "stale-tab": {"status": {"type": "idle"}, "updatedAt": now - 7200},
                # "vanished" unloads between the list and the read: unknown -> busy.
            },
        ),
        "ep:codex-app-server-dispatcher": _FakeAppServer(loaded=[], threads={}),
    }
    monkeypatch.setattr(skew_module, "service_endpoint", lambda name: f"ep:{name}")

    async def open_connection(endpoint):
        return servers[endpoint]

    monkeypatch.setattr(skew_module, "_open_connection", open_connection)

    count = attached_sessions_in_use(
        ("codex-app-server", "codex-app-server-dispatcher"),
        now=now,
        recent_seconds=600.0,
    )
    assert count == 3  # mid-run + typing + vanished; the stale tab may be dropped
    main = servers["ep:codex-app-server"]
    assert main.methods.count("thread/loaded/list") == 2  # paginated
    assert main.methods.count("thread/read") == 4
    assert all(server.closed for server in servers.values())

    # A dispatcher with nothing loaded probes cheaply: no reads at all.
    assert servers["ep:codex-app-server-dispatcher"].methods == [
        "initialize",
        "thread/loaded/list",
    ]


def test_attached_sessions_in_use_is_unknown_when_a_probe_fails(monkeypatch) -> None:
    monkeypatch.setattr(skew_module, "service_endpoint", lambda name: f"ep:{name}")

    async def refuse(endpoint):
        raise ConnectionRefusedError(endpoint)

    monkeypatch.setattr(skew_module, "_open_connection", refuse)
    assert (
        attached_sessions_in_use(("codex-app-server",), now=0.0, recent_seconds=1.0)
        is None
    )


def test_recent_thread_activity_window_env_override(monkeypatch) -> None:
    monkeypatch.delenv(skew_module.RECENT_THREAD_ACTIVITY_ENV, raising=False)
    assert skew_module.recent_thread_activity_seconds() == 600.0
    monkeypatch.setenv(skew_module.RECENT_THREAD_ACTIVITY_ENV, "90")
    assert skew_module.recent_thread_activity_seconds() == 90.0
    monkeypatch.setenv(skew_module.RECENT_THREAD_ACTIVITY_ENV, "soon")
    assert skew_module.recent_thread_activity_seconds() == 600.0


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
    probed: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        skew_module,
        "attached_sessions_in_use",
        lambda names, **_kwargs: probed.append(names) or 0,
    )

    from openbase_coder_cli.services import restart as restart_module

    monkeypatch.setattr(
        restart_module,
        "schedule_restart",
        lambda request, **_kwargs: scheduled.append(request.services),
    )

    first = run_auto_restart_tick()
    assert first["restarted"] == ["codex-app-server"]
    assert scheduled == [("codex-app-server",)]
    assert probed == [("codex-app-server",)]  # only the services being restarted

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
    monkeypatch.setattr(
        skew_module, "attached_sessions_in_use", lambda names, **_kwargs: 2
    )

    from openbase_coder_cli.services import restart as restart_module

    def fail(*_args, **_kwargs):
        raise AssertionError("must not restart while busy")

    monkeypatch.setattr(restart_module, "schedule_restart", fail)

    summary = run_auto_restart_tick()
    assert summary["restarted"] == []
    assert summary["blockers"] == [
        "1 active agent turn(s)",
        "voice session state unknown",
        "2 Codex session(s) in use",
    ]

    # Nothing else busy, but an interactive codex chat is mid-run.
    monkeypatch.setattr(skew_module, "super_agents_active_turn_count", lambda: 0)
    monkeypatch.setattr(skew_module, "voice_session_active", lambda: False)
    monkeypatch.setattr(
        skew_module, "attached_sessions_in_use", lambda names, **_kwargs: 1
    )
    assert run_auto_restart_tick()["blockers"] == ["1 Codex session(s) in use"]


def test_auto_restart_tick_respects_opt_out(monkeypatch) -> None:
    monkeypatch.setenv(skew_module.AUTO_RESTART_ENV, "0")
    monkeypatch.setattr(
        skew_module,
        "collect_codex_version_skews",
        lambda: (_ for _ in ()).throw(AssertionError("must not probe when disabled")),
    )
    assert run_auto_restart_tick() == {
        "skews": [],
        "cli_outdated": [],
        "blockers": [],
        "restarted": [],
    }


def test_version_ordering_is_numeric() -> None:
    assert skew_module.version_is_older("0.160.1", "0.161.0")
    assert skew_module.version_is_older("0.9.0", "0.10.0")  # not lexical
    assert not skew_module.version_is_older("0.161.0", "0.160.1")
    assert not skew_module.version_is_older("0.161.0", "0.161.0")


def test_skew_newer_server_or_shared_daemon_is_advisory_only() -> None:
    stale = _skew()
    assert stale.restart_resolves
    assert not stale.running_is_newer
    assert "restart" not in stale.message.lower()

    newer = CodexVersionSkew(
        service="codex-app-server",
        running_version="0.161.0",
        installed_version="0.160.1",
        installed_path="/opt/codex",
    )
    assert newer.running_is_newer
    assert not newer.restart_resolves
    assert "upgrade the Codex CLI to 0.161.0" in newer.message

    daemon_newer = CodexVersionSkew(
        service="codex-app-server",
        running_version="0.161.0",
        installed_version="0.160.1",
        installed_path="/opt/codex",
        shared_daemon=True,
    )
    assert not daemon_newer.restart_resolves
    assert "shared Codex daemon 0.161.0" in daemon_newer.message
    assert "upgrade the Codex CLI to 0.161.0" in daemon_newer.message

    # The daemon updates itself; an older daemon is still not ours to restart.
    daemon_older = CodexVersionSkew(
        service="codex-app-server",
        running_version="0.160.1",
        installed_version="0.161.0",
        installed_path="/opt/codex",
        shared_daemon=True,
    )
    assert not daemon_older.restart_resolves
    assert "does not restart it" in daemon_older.message


def test_service_version_skew_flags_the_shared_daemon_link(monkeypatch, tmp_path) -> None:
    link = tmp_path / "app-server-control.sock"
    link.symlink_to(tmp_path / "daemon.sock")
    endpoints = {
        "codex-app-server": SimpleNamespace(is_unix=True, socket_path=link),
        "codex-app-server-dispatcher": SimpleNamespace(
            is_unix=True, socket_path=tmp_path / "dispatcher.sock"
        ),
    }
    monkeypatch.setattr(skew_module, "service_endpoint", endpoints.__getitem__)
    monkeypatch.setattr(
        skew_module, "running_codex_app_server_version", lambda endpoint: "0.161.0"
    )
    installed = ("/opt/codex", "0.160.1")

    shared = skew_module.service_version_skew("codex-app-server", installed)
    assert shared is not None and shared.shared_daemon
    assert not shared.restart_resolves

    own = skew_module.service_version_skew("codex-app-server-dispatcher", installed)
    assert own is not None and not own.shared_daemon
    assert own.running_is_newer and not own.restart_resolves


def test_collect_probes_shared_endpoint_without_an_openbase_pid(monkeypatch) -> None:
    from openbase_coder_cli.services import launchd, registry, selection

    monkeypatch.setattr(
        skew_module, "installed_codex_version", lambda: ("/opt/codex", "0.160.1")
    )
    monkeypatch.setattr(
        registry, "find_service", lambda name: SimpleNamespace(name=name)
    )
    monkeypatch.setattr(
        selection, "service_supports_configured_backends", lambda service: True
    )
    monkeypatch.setattr(
        launchd, "launchctl_status", lambda service: {"installed": True, "pid": None}
    )
    probed: list[str] = []

    def fake_skew(name, installed):
        probed.append(name)
        return CodexVersionSkew(
            service=name,
            running_version="0.161.0",
            installed_version=installed[1],
            installed_path=installed[0],
            shared_daemon=True,
        )

    monkeypatch.setattr(skew_module, "service_version_skew", fake_skew)
    skews = skew_module.collect_codex_version_skews()
    # The shared endpoint answers with no Openbase pid; the dispatcher (an
    # Openbase-only instance) is skipped while stopped.
    assert probed == ["codex-app-server"]
    assert [skew.service for skew in skews] == ["codex-app-server"]


def test_auto_restart_tick_never_restarts_an_advisory_skew(monkeypatch, caplog) -> None:
    import logging

    monkeypatch.setattr(skew_module, "_last_scheduled", {})
    monkeypatch.setattr(skew_module, "_last_advised", {})
    daemon = CodexVersionSkew(
        service="codex-app-server",
        running_version="0.161.0",
        installed_version="0.160.1",
        installed_path="/opt/codex",
        shared_daemon=True,
    )
    monkeypatch.setattr(skew_module, "collect_codex_version_skews", lambda: [daemon])
    monkeypatch.setattr(
        skew_module,
        "super_agents_active_turn_count",
        lambda: pytest.fail("no restart means no busy probe"),
    )

    from openbase_coder_cli.services import restart as restart_module

    monkeypatch.setattr(
        restart_module,
        "schedule_restart",
        lambda *_a, **_k: pytest.fail("must never restart for the shared daemon"),
    )

    with caplog.at_level(logging.WARNING, logger=skew_module.__name__):
        first = run_auto_restart_tick()
        second = run_auto_restart_tick()
    assert first["skews"] == [] and first["restarted"] == [] and first["blockers"] == []
    assert first["cli_outdated"] == ["codex-app-server"]
    assert second["cli_outdated"] == ["codex-app-server"]
    # Logged once per version pair, not on every tick.
    warned = [r for r in caplog.records if "no_restart" in r.getMessage()]
    assert len(warned) == 1
    assert "upgrade the Codex CLI to 0.161.0" in warned[0].getMessage()

    # A stale Openbase-owned dispatcher alongside it still restarts.
    monkeypatch.setattr(
        skew_module,
        "collect_codex_version_skews",
        lambda: [daemon, _skew(service="codex-app-server-dispatcher")],
    )
    monkeypatch.setattr(skew_module, "super_agents_active_turn_count", lambda: 0)
    monkeypatch.setattr(skew_module, "voice_session_active", lambda: False)
    monkeypatch.setattr(skew_module, "attached_sessions_in_use", lambda names, **_k: 0)
    scheduled: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        restart_module,
        "schedule_restart",
        lambda request, **_k: scheduled.append(request.services),
    )
    third = run_auto_restart_tick()
    assert third["restarted"] == ["codex-app-server-dispatcher"]
    assert third["cli_outdated"] == ["codex-app-server"]
    assert scheduled == [("codex-app-server-dispatcher",)]
