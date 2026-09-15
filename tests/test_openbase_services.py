from __future__ import annotations

import pytest

from openbase_coder_cli.services import openbase_services
from openbase_coder_cli.services.definitions import SERVICES, ServiceDefinition
from openbase_coder_cli.services.restart import RestartPlan


@pytest.fixture(autouse=True)
def _configured_backend(monkeypatch):
    monkeypatch.setattr(openbase_services, "configured_coding_backend", lambda: "codex")


def test_settings_service_payload_includes_all_defined_openbase_services(monkeypatch):
    statuses = {
        service.name: {
            "installed": True,
            "pid": 1000 + index,
            "last_exit_code": None,
        }
        for index, service in enumerate(SERVICES)
    }

    def fake_launchctl_status(service: ServiceDefinition) -> dict:
        return statuses[service.name]

    monkeypatch.setattr(openbase_services, "launchctl_status", fake_launchctl_status)

    payload = openbase_services.list_openbase_services_payload()

    assert [service["name"] for service in payload["services"]] == [
        service.name for service in SERVICES
    ]
    assert {
        "livekit-server",
        "codex-app-server",
        "livekit-agent",
        "django-cli",
    }.issubset({service["name"] for service in payload["services"]})
    assert all(service["installed"] for service in payload["services"])
    assert all(service["running"] for service in payload["services"])


def test_settings_marks_services_for_other_backends_optional(monkeypatch):
    monkeypatch.setattr(
        openbase_services,
        "launchctl_status",
        lambda _service: {"installed": False, "pid": None, "last_exit_code": None},
    )
    monkeypatch.setattr(
        openbase_services,
        "configured_coding_backend",
        lambda: "openbase_cloud",
    )

    payload = openbase_services.list_openbase_services_payload()
    services = {service["name"]: service for service in payload["services"]}

    assert services["codex-app-server"]["optional"] is True
    assert services["livekit-server"]["optional"] is False


def test_restart_payload_schedules_everything(monkeypatch):
    requests = []
    statuses = {
        service.name: {
            "installed": True,
            "pid": 1000 + index,
            "last_exit_code": None,
        }
        for index, service in enumerate(SERVICES)
    }

    def fake_launchctl_status(service: ServiceDefinition) -> dict:
        return statuses[service.name]

    monkeypatch.setattr(openbase_services, "launchctl_status", fake_launchctl_status)

    def fake_schedule(request, **kwargs):
        requests.append((request, kwargs))
        return RestartPlan(
            services=tuple(service.name for service in SERVICES),
            recreate_dispatcher=request.recreate_dispatcher,
            interrupts_voice=True,
            delay_seconds=request.delay_seconds,
        )

    monkeypatch.setattr(openbase_services, "schedule_restart", fake_schedule)

    payload = openbase_services.schedule_openbase_restart_payload()

    assert payload["scheduled"] is True
    assert payload["restart"]["services"] == [service.name for service in SERVICES]
    assert requests[0][0].services == ()
    assert requests[0][1] == {"emit_cli_warning": False}


def test_restart_payload_rejects_super_agents_mcp_target(monkeypatch):
    requests = []
    statuses = {
        service.name: {
            "installed": True,
            "pid": 1000 + index,
            "last_exit_code": None,
        }
        for index, service in enumerate(SERVICES)
    }

    def fake_launchctl_status(service: ServiceDefinition) -> dict:
        return statuses[service.name]

    monkeypatch.setattr(openbase_services, "launchctl_status", fake_launchctl_status)

    def fake_schedule(request, **kwargs):
        requests.append((request, kwargs))
        raise openbase_services.click.ClickException(
            "Unknown restart target 'super-agents-mcp'."
        )

    monkeypatch.setattr(openbase_services, "schedule_restart", fake_schedule)

    try:
        openbase_services.schedule_openbase_restart_payload(
            service_name="super-agents-mcp"
        )
    except openbase_services.click.ClickException as exc:
        assert "Unknown restart target 'super-agents-mcp'" in str(exc)
    else:
        raise AssertionError("super-agents-mcp should not be accepted")

    assert requests[0][0].services == ("super-agents-mcp",)


def test_livekit_service_restart_warns_before_action(monkeypatch):
    requests = []
    statuses = {
        service.name: {
            "installed": True,
            "pid": 1000 + index,
            "last_exit_code": None,
        }
        for index, service in enumerate(SERVICES)
    }

    def fake_launchctl_status(service: ServiceDefinition) -> dict:
        return statuses[service.name]

    monkeypatch.setattr(openbase_services, "launchctl_status", fake_launchctl_status)

    def fake_schedule(request, **kwargs):
        requests.append((request, kwargs))
        return RestartPlan(
            services=request.services,
            recreate_dispatcher=False,
            interrupts_voice=True,
            delay_seconds=request.delay_seconds,
        )

    monkeypatch.setattr(openbase_services, "schedule_restart", fake_schedule)

    payload = openbase_services.run_openbase_service_action("livekit-agent", "restart")

    assert payload["scheduled"] is True
    assert payload["restart"]["services"] == ["livekit-agent"]
    assert requests[0][1] == {"emit_cli_warning": False}


def test_start_reloads_launchd_service_definition(monkeypatch):
    installs = []
    statuses = {
        service.name: {
            "installed": True,
            "pid": None,
            "last_exit_code": None,
        }
        for service in SERVICES
    }

    monkeypatch.setattr(openbase_services, "require_installation", lambda: object())
    monkeypatch.setattr(
        openbase_services,
        "launchctl_status",
        lambda service: statuses[service.name],
    )
    monkeypatch.setattr(
        openbase_services,
        "warn_before_voice_interruption",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        openbase_services,
        "install_service",
        lambda _config, service: installs.append(service.name),
    )

    payload = openbase_services.run_openbase_service_action("livekit-agent", "start")

    assert installs == ["livekit-agent"]
    assert payload["scheduled"] is False


def test_codex_service_restart_does_not_warn(monkeypatch):
    requests = []
    statuses = {
        service.name: {
            "installed": True,
            "pid": 1000 + index,
            "last_exit_code": None,
        }
        for index, service in enumerate(SERVICES)
    }

    def fake_launchctl_status(service: ServiceDefinition) -> dict:
        return statuses[service.name]

    monkeypatch.setattr(openbase_services, "launchctl_status", fake_launchctl_status)

    def fake_schedule(request, **kwargs):
        requests.append((request, kwargs))
        return RestartPlan(
            services=request.services,
            recreate_dispatcher=False,
            interrupts_voice=False,
            delay_seconds=request.delay_seconds,
        )

    monkeypatch.setattr(openbase_services, "schedule_restart", fake_schedule)

    payload = openbase_services.run_openbase_service_action(
        "codex-app-server", "restart"
    )

    assert payload["scheduled"] is True
    assert payload["restart"]["services"] == ["codex-app-server"]
