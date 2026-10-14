"""Backend-scoped services follow the primary coding backend on containers."""

from __future__ import annotations

import pytest

from openbase_coder_cli.services import backend_services, launchd
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.services.registry import find_service

CODEX_SERVICES = ("codex-app-server", "codex-app-server-dispatcher")


@pytest.fixture
def container(monkeypatch):
    monkeypatch.setattr(
        "openbase_coder_cli.dispatcher_config.backend_model", lambda *a, **k: None
    )
    actions: list[str] = []
    running: set[str] = set()
    monkeypatch.setattr(launchd, "_external_supervisor", lambda: True)
    monkeypatch.setattr(
        launchd,
        "launchctl_status",
        lambda svc: {"installed": True, "pid": "1" if svc.name in running else None},
    )
    monkeypatch.setattr(
        launchd,
        "install_service",
        lambda _config, svc: actions.append(f"start:{svc.name}"),
    )
    monkeypatch.setattr(
        launchd, "launchctl_bootout", lambda svc: actions.append(f"stop:{svc.name}")
    )
    return actions, running


def _primary(monkeypatch, backend: str) -> None:
    monkeypatch.setattr(backend_services, "configured_coding_backend", lambda: backend)


def test_switch_to_codex_starts_both_codex_services(container, monkeypatch):
    actions, _running = container
    _primary(monkeypatch, "codex")

    result = backend_services.reconcile_backend_services(InstallationConfig())

    assert actions == [f"start:{name}" for name in CODEX_SERVICES]
    assert result == {"started": list(CODEX_SERVICES), "stopped": []}


def test_running_codex_services_are_left_alone_on_codex(container, monkeypatch):
    actions, running = container
    running.update(CODEX_SERVICES)
    _primary(monkeypatch, "codex")

    assert backend_services.reconcile_backend_services(InstallationConfig()) == {
        "started": [],
        "stopped": [],
    }
    assert actions == []


def test_switch_back_to_claude_stops_running_codex_services(container, monkeypatch):
    """A Claude-only workspace keeps the Codex app servers down to save memory."""
    actions, running = container
    running.add("codex-app-server")
    _primary(monkeypatch, "claude_code")

    result = backend_services.reconcile_backend_services(InstallationConfig())

    assert actions == [f"stop:{name}" for name in CODEX_SERVICES]
    assert result == {"started": [], "stopped": ["codex-app-server"]}


def test_openbase_cloud_primary_also_stops_codex(container, monkeypatch):
    actions, running = container
    running.update(CODEX_SERVICES)
    _primary(monkeypatch, "openbase_cloud")

    backend_services.reconcile_backend_services(InstallationConfig())

    assert actions == [f"stop:{name}" for name in CODEX_SERVICES]


def test_launchd_installs_are_never_touched(container, monkeypatch):
    actions, _running = container
    monkeypatch.setattr(launchd, "_external_supervisor", lambda: False)
    _primary(monkeypatch, "codex")

    assert backend_services.reconcile_backend_services(InstallationConfig()) == {
        "started": [],
        "stopped": [],
    }
    assert actions == []
    assert backend_services.reconcile_backend_services_in_background() is None


def test_restart_plan_skips_the_other_backends_services_on_containers(monkeypatch):
    monkeypatch.setattr(launchd, "_external_supervisor", lambda: True)
    _primary(monkeypatch, "claude_code")
    assert backend_services.restart_plan_includes(find_service("django-cli"))
    assert not backend_services.restart_plan_includes(find_service("codex-app-server"))

    _primary(monkeypatch, "codex")
    assert backend_services.restart_plan_includes(find_service("codex-app-server"))

    monkeypatch.setattr(launchd, "_external_supervisor", lambda: False)
    _primary(monkeypatch, "claude_code")
    assert backend_services.restart_plan_includes(find_service("codex-app-server"))


def test_background_reconcile_runs_on_a_thread(container, monkeypatch):
    actions, _running = container
    _primary(monkeypatch, "codex")
    monkeypatch.setattr(
        backend_services, "require_installation", lambda: InstallationConfig()
    )

    thread = backend_services.reconcile_backend_services_in_background()

    assert thread is not None
    thread.join(timeout=10)
    assert actions == [f"start:{name}" for name in CODEX_SERVICES]


def test_crashed_codex_services_stay_stopped_after_switch(container, monkeypatch):
    actions, _running = container
    _primary(monkeypatch, "claude_code")
    result = backend_services.reconcile_backend_services(InstallationConfig())
    assert actions == [f"stop:{name}" for name in CODEX_SERVICES]
    assert result == {"started": [], "stopped": []}


def test_codex_dispatcher_still_needs_services_with_claude_workers(
    container, monkeypatch
):
    actions, _running = container
    _primary(monkeypatch, "claude_code")
    monkeypatch.setattr(
        "openbase_coder_cli.dispatcher_config.backend_model",
        lambda *a, **k: "gpt-6.1-sol",
    )
    backend_services.reconcile_backend_services(InstallationConfig())
    assert actions == [f"start:{name}" for name in CODEX_SERVICES]


def test_background_switches_serialize_before_reading_running_state(
    container, monkeypatch
):
    import threading

    actions, running = container
    _primary(monkeypatch, "codex")
    entered, release = threading.Event(), threading.Event()

    def install(_config, svc):
        actions.append(f"start:{svc.name}")
        entered.set()
        assert release.wait(timeout=5)
        running.add(svc.name)

    monkeypatch.setattr(launchd, "install_service", install)
    monkeypatch.setattr(backend_services, "require_installation", InstallationConfig)
    first = backend_services.reconcile_backend_services_in_background()
    assert entered.wait(timeout=5)
    second = backend_services.reconcile_backend_services_in_background()
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)
    assert not first.is_alive() and not second.is_alive()
    assert actions == [f"start:{name}" for name in CODEX_SERVICES]
