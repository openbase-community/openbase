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


def test_codex_provider_is_ready_when_newer_than_installed_cli(monkeypatch):
    from openbase_coder_cli.services import codex_version_skew as skew_module

    monkeypatch.setattr(
        skew_module, "installed_codex_version", lambda: ("/opt/codex", "0.160.1")
    )
    monkeypatch.setattr(skew_module, "service_endpoint", lambda name: f"ep:{name}")
    versions = {"ep:codex-app-server": "0.161.0"}
    monkeypatch.setattr(
        skew_module, "running_codex_app_server_version", versions.get
    )
    assert readiness.provider_ready(definition("codex-app-server")) is True

    versions["ep:codex-app-server"] = "0.160.0"  # the stale pre-restart instance
    assert readiness.provider_ready(definition("codex-app-server")) is False
    versions["ep:codex-app-server"] = None  # not answering yet
    assert readiness.provider_ready(definition("codex-app-server")) is False


def test_shared_codex_daemon_is_a_ready_provider_at_any_version(monkeypatch, tmp_path):
    from openbase_coder_cli.services import codex_version_skew as skew_module

    link = tmp_path / "app-server-control.sock"
    link.symlink_to(tmp_path / "daemon.sock")
    monkeypatch.setattr(
        skew_module, "installed_codex_version", lambda: ("/opt/codex", "0.161.0")
    )
    monkeypatch.setattr(
        skew_module,
        "service_endpoint",
        lambda name: SimpleNamespace(is_unix=True, socket_path=link),
    )
    monkeypatch.setattr(
        skew_module, "running_codex_app_server_version", lambda endpoint: "0.160.1"
    )
    assert readiness.provider_ready(definition("codex-app-server")) is True
