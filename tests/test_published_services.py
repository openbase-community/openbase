from __future__ import annotations

import importlib
import plistlib
from dataclasses import replace

import pytest
from aiohttp import ClientSession, web
from click.testing import CliRunner

from openbase_coder_cli.services.published_services import PublishedService

service_cli = importlib.import_module("openbase_coder_cli.cli.service")
published = importlib.import_module("openbase_coder_cli.services.published_services")
gateway = importlib.import_module("openbase_coder_cli.services.service_gateway")


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
    path = tmp_path / "published-services.json"
    monkeypatch.setattr(published, "PUBLISHED_SERVICES_PATH", path)
    return path


def test_name_and_tailnet_port_validation_reject_mdns_and_common_ports():
    with pytest.raises(ValueError, match="multicast DNS"):
        published.validate_name("demo.local")
    with pytest.raises(ValueError, match="dynamic/private"):
        published.validate_tailnet_port(3000)
    assert published.validate_name("docs-preview") == "docs-preview"
    assert published.validate_tailnet_port(52807) == 52807


def test_registry_round_trip_uses_private_permissions(isolated_registry):
    item = PublishedService("docs", 3000, 52807, 52808, persistent=True)

    published.save_services([item])

    assert published.load_services() == [item]
    assert isolated_registry.stat().st_mode & 0o777 == 0o600


def test_gateway_strips_memorable_prefix_but_keeps_root_relative_paths():
    assert gateway.upstream_path("/docs", "docs") == "/"
    assert gateway.upstream_path("/docs/api?q=1", "docs") == "/api?q=1"
    assert gateway.upstream_path("/assets/app.js", "docs") == "/assets/app.js"


@pytest.mark.asyncio
async def test_gateway_proxies_named_and_root_relative_paths(isolated_registry):
    async def echo(request):
        return web.Response(text=request.path_qs)

    backend = web.Application()
    backend.router.add_route("*", "/{path:.*}", echo)
    backend_runner = web.AppRunner(backend)
    await backend_runner.setup()
    backend_site = web.TCPSite(backend_runner, "127.0.0.1", 0)
    await backend_site.start()
    backend_port = backend_site._server.sockets[0].getsockname()[1]
    published.save_services([PublishedService("docs", backend_port, 52807, 52808)])

    proxy_runner = web.AppRunner(gateway.create_app("docs"))
    await proxy_runner.setup()
    proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", 0)
    await proxy_site.start()
    proxy_port = proxy_site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as client:
            named = await client.get(f"http://127.0.0.1:{proxy_port}/docs/api?q=1")
            root_relative = await client.get(
                f"http://127.0.0.1:{proxy_port}/assets/app.js"
            )
            assert await named.text() == "/api?q=1"
            assert await root_relative.text() == "/assets/app.js"
    finally:
        await proxy_runner.cleanup()
        await backend_runner.cleanup()


def test_publish_defaults_noninteractive_to_session_and_applies_route(
    monkeypatch, isolated_registry
):
    applied: list[PublishedService] = []
    stopped: list[PublishedService] = []
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(service_cli, "allocate_ports", lambda _port: (52807, 52808))
    monkeypatch.setattr(service_cli, "start_ephemeral_gateway", lambda _item: 4123)
    monkeypatch.setattr(service_cli, "gateway_healthy", lambda _item: True)
    monkeypatch.setattr(service_cli, "apply_route", applied.append)
    monkeypatch.setattr(service_cli, "stop_gateway", stopped.append)
    monkeypatch.setattr(
        service_cli,
        "service_url",
        lambda item: f"http://mac.tailnet.example:{item.tailnet_port}/{item.name}/",
    )

    result = CliRunner().invoke(service_cli.service, ["publish", "docs", "3000"])

    assert result.exit_code == 0, result.output
    assert "Persistence was not enabled" in result.output
    assert "http://mac.tailnet.example:52807/docs/" in result.output
    assert applied == [
        PublishedService("docs", 3000, 52807, 52808, persistent=False, pid=4123)
    ]
    assert stopped == []
    assert published.load_services() == applied


def test_publish_persistence_is_explicit(monkeypatch, isolated_registry):
    installed: list[PublishedService] = []
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(service_cli, "allocate_ports", lambda _port: (52807, 52808))
    monkeypatch.setattr(service_cli, "install_launchd_service", installed.append)
    monkeypatch.setattr(service_cli, "gateway_healthy", lambda _item: True)
    monkeypatch.setattr(service_cli, "apply_route", lambda _item: None)
    monkeypatch.setattr(
        service_cli, "service_url", lambda _item: "http://host:52807/docs/"
    )

    result = CliRunner().invoke(
        service_cli.service, ["publish", "docs", "3000", "--persist"]
    )

    assert result.exit_code == 0, result.output
    assert installed == [PublishedService("docs", 3000, 52807, 52808, True)]
    assert "explicit opt-in" in result.output


def test_launchd_persistence_uses_gateway_module_and_keepalive(monkeypatch, tmp_path):
    launchd_dir = tmp_path / "launchd"
    plist_dir = tmp_path / "LaunchAgents"
    log_dir = tmp_path / "logs"
    commands = []
    monkeypatch.setattr(published.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(published, "LAUNCHD_WRAPPER_DIR", launchd_dir)
    monkeypatch.setattr(published, "PLIST_DIR", plist_dir)
    monkeypatch.setattr(published, "DEFAULT_LOG_DIR", log_dir)
    monkeypatch.setattr(
        published,
        "_launchctl",
        lambda *args: commands.append(args)
        or type("Result", (), {"returncode": 0, "stderr": ""})(),
    )
    item = PublishedService("docs", 3000, 52807, 52808, True)

    published.install_launchd_service(item)

    wrapper = (launchd_dir / "published-service-docs.sh").read_text()
    with (plist_dir / "com.openbase.coder.published-service.docs.plist").open(
        "rb"
    ) as stream:
        plist = plistlib.load(stream)
    assert "openbase_coder_cli.services.service_gateway" in wrapper
    assert "--name docs" in wrapper
    assert plist["RunAtLoad"] is True
    assert plist["KeepAlive"] is True
    assert commands[-1][0] == "bootstrap"


def test_publish_rolls_back_registry_and_gateway_on_route_failure(
    monkeypatch, isolated_registry
):
    stopped: list[PublishedService] = []
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(service_cli, "allocate_ports", lambda _port: (52807, 52808))
    monkeypatch.setattr(service_cli, "start_ephemeral_gateway", lambda _item: 99)
    monkeypatch.setattr(service_cli, "gateway_healthy", lambda _item: True)
    monkeypatch.setattr(
        service_cli,
        "apply_route",
        lambda _item: (_ for _ in ()).throw(RuntimeError("no VPN")),
    )
    monkeypatch.setattr(service_cli, "stop_gateway", stopped.append)

    result = CliRunner().invoke(
        service_cli.service, ["publish", "docs", "3000", "--no-persist"]
    )

    assert result.exit_code != 0
    assert "no VPN" in result.output
    assert published.load_services() == []
    assert stopped[0].pid == 99


def test_unpublish_removes_route_before_stopping(monkeypatch, isolated_registry):
    item = PublishedService("docs", 3000, 52807, 52808, False, 99)
    published.save_services([item])
    events: list[str] = []
    monkeypatch.setattr(
        service_cli, "remove_route", lambda _item: events.append("route")
    )
    monkeypatch.setattr(
        service_cli, "stop_gateway", lambda _item: events.append("gateway")
    )

    result = CliRunner().invoke(service_cli.service, ["unpublish", "docs"])

    assert result.exit_code == 0, result.output
    assert events == ["route", "gateway"]
    assert published.load_services() == []


def test_only_persistent_rules_are_restored(isolated_registry):
    persistent = PublishedService("docs", 3000, 52807, 52808, True)
    session = replace(persistent, name="preview", tailnet_port=52809, persistent=False)
    published.save_services([persistent, session])

    assert published.published_serve_rules(persistent_only=True) == [
        persistent.serve_rule()
    ]


def test_remove_serve_uses_exact_listener_flags(monkeypatch):
    provider = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    commands = []
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(provider, "is_netmesh", lambda: False)
    monkeypatch.setattr(provider, "tailscale_bin", lambda: "/usr/bin/tailscale")
    monkeypatch.setattr(
        provider,
        "_run",
        lambda command: (
            commands.append(command)
            or type("Result", (), {"returncode": 0, "stderr": "", "stdout": ""})()
        ),
    )

    provider.remove_serve("http", 52807)

    assert commands == [["/usr/bin/tailscale", "serve", "--http=52807", "off"]]


def test_openbase_direct_is_rejected_without_applying_a_route(monkeypatch):
    provider = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: True)

    with pytest.raises(RuntimeError, match="Openbase Direct"):
        published.apply_route(PublishedService("docs", 3000, 52807, 52808))
