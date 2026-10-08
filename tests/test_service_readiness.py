"""A provider must serve the new endpoint before consumers restart."""

from types import SimpleNamespace

import click
import pytest

from openbase_coder_cli.services import launchd, readiness
from openbase_coder_cli.services.definitions import ServiceDefinition
from openbase_coder_cli.services.installation import InstallationConfig


def definition(name, *dependents):
    return ServiceDefinition(
        name=name,
        description=name,
        command_template=name,
        workdir_template="",
        restart_dependents=dependents,
    )


def test_provider_delay_is_waited_out(monkeypatch):
    states = iter([False, False, True])
    waits = []
    monkeypatch.setattr(readiness, "provider_ready", lambda _: next(states))
    monkeypatch.setattr(
        readiness, "time", SimpleNamespace(monotonic=lambda: 0, sleep=waits.append)
    )
    readiness.wait_for_provider(definition("provider", "consumer"))
    assert waits == [0.25, 0.25]


def test_failed_provider_stops_install_batch_before_consumers(monkeypatch):
    provider, consumer = definition("provider", "consumer"), definition("consumer")
    monkeypatch.setattr(launchd, "default_services", lambda *_: [consumer, provider])
    monkeypatch.setattr(
        launchd, "include_installed_optional_services", lambda services, _: services
    )
    monkeypatch.setattr(launchd, "_ensure_launchd_paths", lambda: None)
    monkeypatch.setattr(launchd, "_selected_backend", lambda _: "codex")
    monkeypatch.setattr(launchd.tp, "provider", lambda: "netmesh-tsnet")
    monkeypatch.setattr(launchd, "_resolve_binaries", lambda *a: {})
    monkeypatch.setattr(launchd, "remove_service", lambda _: False)
    monkeypatch.setattr(launchd, "_write_service_files", lambda *a: False)
    activated = []
    monkeypatch.setattr(
        launchd,
        "_activate_service",
        lambda svc, _: activated.append(svc.name) or "Loaded",
    )
    monkeypatch.setattr(readiness, "provider_ready", lambda _: False)
    monkeypatch.setattr(
        launchd,
        "wait_for_provider",
        lambda svc: readiness.wait_for_provider(svc, timeout=0),
    )
    with pytest.raises(click.ClickException, match="did not become ready"):
        launchd.install_all_services(InstallationConfig(standalone=True))
    assert activated == ["provider"]


@pytest.mark.parametrize(
    "running,expected", [(None, False), ("1.0", False), ("2.0", True)]
)
def test_codex_provider_requires_handshake_at_installed_version(
    monkeypatch, running, expected
):
    from openbase_coder_cli.services import codex_version_skew as skew

    monkeypatch.setattr(skew, "installed_codex_version", lambda: ("fixture", "2.0"))
    monkeypatch.setattr(skew, "service_endpoint", lambda _: "fixture")
    monkeypatch.setattr(skew, "running_codex_app_server_version", lambda _: running)
    assert readiness.provider_ready(definition("codex-app-server")) is expected
