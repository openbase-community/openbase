from __future__ import annotations

import importlib
import json
import plistlib
import sys
from dataclasses import replace

import pytest
from aiohttp import ClientSession, WSMsgType, web
from click.testing import CliRunner

from openbase_coder_cli.services.published_services import PublishedService

service_cli = importlib.import_module("openbase_coder_cli.cli.service")
published = importlib.import_module("openbase_coder_cli.services.published_services")
gateway = importlib.import_module("openbase_coder_cli.services.service_gateway")
routes = importlib.import_module("openbase_coder_cli.services.published_service_routes")


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
    payload = json.loads(isolated_registry.read_text())
    assert payload["version"] == 4
    assert payload["services"][0]["mode"] == "dynamic"


def test_registry_v1_is_loaded_as_dynamic_and_upgraded(isolated_registry):
    isolated_registry.write_text(
        json.dumps(
            {
                "version": 1,
                "services": [
                    {
                        "name": "docs",
                        "local_port": 3000,
                        "tailnet_port": 52807,
                        "proxy_port": 52808,
                        "persistent": False,
                    }
                ],
            }
        )
    )

    registry = published.load_registry()
    published.save_registry(registry)

    assert registry.services[0].mode == published.MODE_DYNAMIC
    assert json.loads(isolated_registry.read_text())["version"] == 4


def test_service_urls_preserve_dynamic_prefix_and_root_mount_hostnames(monkeypatch):
    provider = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    monkeypatch.setattr(
        provider,
        "status_json",
        lambda: {"Self": {"DNSName": "mac.openbase.test.", "TailscaleIPs": []}},
    )
    dynamic = PublishedService("docs", 3000, 52807, 52808)
    hostname = PublishedService(
        "crm",
        4000,
        80,
        52809,
        mode="hostname",
        hostname="crm.mac.openbase.test",
    )

    assert published.service_url(dynamic) == "http://mac.openbase.test:52807/docs/"
    assert published.service_url(hostname) == "http://crm.mac.openbase.test/"


@pytest.mark.asyncio
async def test_hostname_gateway_preserves_raw_paths_and_queries(isolated_registry):
    async def echo(request):
        return web.Response(text=request.path_qs)

    backend = web.Application()
    backend.router.add_route("*", "/{path:.*}", echo)
    backend_runner = web.AppRunner(backend)
    await backend_runner.setup()
    backend_site = web.TCPSite(backend_runner, "127.0.0.1", 0)
    await backend_site.start()
    backend_port = backend_site._server.sockets[0].getsockname()[1]
    published.save_services(
        [
            PublishedService(
                "docs",
                backend_port,
                80,
                52808,
                mode="hostname",
                hostname="docs.mac.netmesh.openbase.cloud",
                node_id="7",
            )
        ]
    )

    proxy_runner = web.AppRunner(gateway.create_app("docs"))
    await proxy_runner.setup()
    proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", 0)
    await proxy_site.start()
    proxy_port = proxy_site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as client:
            named = await client.get(f"http://127.0.0.1:{proxy_port}/docs/api?q=1")
            root_relative = await client.get(f"http://127.0.0.1:{proxy_port}/")
            assert await named.text() == "/docs/api?q=1"
            assert await root_relative.text() == "/"
    finally:
        await proxy_runner.cleanup()
        await backend_runner.cleanup()


@pytest.mark.asyncio
async def test_dynamic_gateway_keeps_legacy_name_prefix_contract(isolated_registry):
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


@pytest.mark.asyncio
async def test_gateway_streams_and_hardens_forwarded_headers(isolated_registry):
    seen_headers = {}

    async def stream(request):
        seen_headers.update(request.headers)
        response = web.StreamResponse(status=206)
        response.headers.add("Set-Cookie", "first=1; Path=/")
        response.headers.add("Set-Cookie", "second=2; Path=/")
        await response.prepare(request)
        await response.write(b"first")
        await response.write(b"second")
        await response.write_eof()
        return response

    async def upload(request):
        chunks = []
        async for chunk in request.content.iter_any():
            chunks.append(chunk)
        return web.Response(text=str(sum(len(chunk) for chunk in chunks)))

    async def upload_body():
        yield b"a" * 1024
        yield b"b" * 2048

    backend = web.Application()
    backend.router.add_post("/upload", upload)
    backend.router.add_route("*", "/{path:.*}", stream)
    backend_runner = web.AppRunner(backend)
    await backend_runner.setup()
    backend_site = web.TCPSite(backend_runner, "127.0.0.1", 0)
    await backend_site.start()
    backend_port = backend_site._server.sockets[0].getsockname()[1]
    item = PublishedService("docs", backend_port, 52807, 52808)
    published.save_services([item])

    proxy_runner = web.AppRunner(gateway.create_app("docs"))
    await proxy_runner.setup()
    proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", 0)
    await proxy_site.start()
    proxy_port = proxy_site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as client:
            response = await client.get(
                f"http://127.0.0.1:{proxy_port}/assets/app.js",
                headers={
                    "Forwarded": "for=attacker",
                    "X-Forwarded-Host": "attacker.example",
                    "X-Forwarded-Prefix": "/wrong",
                    "Connection": "X-Remove-Me",
                    "X-Remove-Me": "secret",
                },
            )
            assert response.status == 206
            assert await response.read() == b"firstsecond"
            assert response.headers.getall("Set-Cookie") == [
                "first=1; Path=/",
                "second=2; Path=/",
            ]
            uploaded = await client.post(
                f"http://127.0.0.1:{proxy_port}/upload", data=upload_body()
            )
            assert await uploaded.text() == "3072"
        assert "X-Forwarded-Prefix" not in seen_headers
        assert seen_headers["X-Forwarded-Host"] == f"127.0.0.1:{proxy_port}"
        assert seen_headers["X-Forwarded-Proto"] == "http"
        assert seen_headers["X-Forwarded-Port"] == "52807"
        assert seen_headers["X-Forwarded-For"] == "127.0.0.1"
        assert "Forwarded" not in seen_headers
        assert "X-Remove-Me" not in seen_headers
    finally:
        await proxy_runner.cleanup()
        await backend_runner.cleanup()


@pytest.mark.asyncio
async def test_gateway_forwards_websockets_without_a_path_prefix(isolated_registry):
    async def websocket(request):
        response = web.WebSocketResponse()
        await response.prepare(request)
        async for message in response:
            if message.type == WSMsgType.TEXT:
                await response.send_str(f"echo:{message.data}")
        return response

    backend = web.Application()
    backend.router.add_get("/socket", websocket)
    backend_runner = web.AppRunner(backend)
    await backend_runner.setup()
    backend_site = web.TCPSite(backend_runner, "127.0.0.1", 0)
    await backend_site.start()
    backend_port = backend_site._server.sockets[0].getsockname()[1]
    published.save_services([PublishedService("chat", backend_port, 52807, 52808)])
    proxy_runner = web.AppRunner(gateway.create_app("chat"))
    await proxy_runner.setup()
    proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", 0)
    await proxy_site.start()
    proxy_port = proxy_site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as client:
            async with client.ws_connect(
                f"http://127.0.0.1:{proxy_port}/socket"
            ) as connection:
                await connection.send_str("hello")
                message = await connection.receive(timeout=2)
                assert message.data == "echo:hello"
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
    monkeypatch.setattr(
        service_cli,
        "apply_route",
        lambda item, **_kwargs: applied.append(item),
    )
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


def test_gateway_health_retries_until_proxy_accepts(monkeypatch):
    item = PublishedService("docs", 3000, 52807, 52808)
    attempts = 0

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    def connect(address, timeout):
        nonlocal attempts
        attempts += 1
        assert address == ("127.0.0.1", 52808)
        assert timeout <= 0.2
        if attempts < 3:
            raise ConnectionRefusedError("starting")
        return Connection()

    monkeypatch.setattr(published.socket, "create_connection", connect)
    monkeypatch.setattr(published.time, "sleep", lambda _seconds: None)

    assert published.gateway_healthy(item)
    assert attempts == 3


def test_publish_persistence_is_explicit(monkeypatch, isolated_registry):
    installed: list[PublishedService] = []
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(service_cli, "allocate_ports", lambda _port: (52807, 52808))
    monkeypatch.setattr(service_cli, "install_launchd_service", installed.append)
    monkeypatch.setattr(service_cli, "gateway_healthy", lambda _item: True)
    monkeypatch.setattr(service_cli, "apply_route", lambda _item, **_kwargs: None)
    monkeypatch.setattr(
        service_cli, "service_url", lambda _item: "http://host:52807/docs/"
    )

    result = CliRunner().invoke(
        service_cli.service,
        ["publish", "docs", "3000", "--persist", "--mode", "dynamic"],
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
        lambda *args: (
            commands.append(args)
            or type("Result", (), {"returncode": 0, "stderr": ""})()
        ),
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
        lambda _item, **_kwargs: (_ for _ in ()).throw(RuntimeError("no VPN")),
    )
    monkeypatch.setattr(service_cli, "stop_gateway", stopped.append)

    result = CliRunner().invoke(
        service_cli.service,
        ["publish", "docs", "3000", "--no-persist", "--mode", "dynamic"],
    )

    assert result.exit_code != 0
    assert "no VPN" in result.output
    assert published.load_services() == []
    assert stopped[0].pid == 99


def test_publish_compensates_serve_when_final_registry_save_fails(
    monkeypatch, isolated_registry
):
    real_save = published.save_registry
    save_calls = 0
    compensated = []

    def fail_final_save(registry):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 3:
            raise OSError("forced final save failure")
        real_save(registry)

    monkeypatch.setattr(service_cli, "save_registry", fail_final_save)
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(service_cli, "allocate_ports", lambda _port: (52807, 52808))
    monkeypatch.setattr(
        service_cli, "service_url", lambda _item: "http://host:52807/docs/"
    )
    monkeypatch.setattr(service_cli, "start_ephemeral_gateway", lambda _item: 99)
    monkeypatch.setattr(service_cli, "gateway_healthy", lambda _item: True)
    monkeypatch.setattr(service_cli, "apply_route", lambda _item, **_kwargs: "new-hash")
    monkeypatch.setattr(
        service_cli,
        "remove_route",
        lambda _item, **kwargs: compensated.append(kwargs) or "old-hash",
    )
    monkeypatch.setattr(service_cli, "stop_gateway", lambda _item: None)

    result = CliRunner().invoke(
        service_cli.service,
        ["publish", "docs", "3000", "--mode", "dynamic", "--no-persist"],
    )

    assert result.exit_code != 0
    assert "forced final save failure" in result.output
    assert compensated[0]["last_applied_hash"] == "new-hash"
    assert published.load_services() == []


def test_unpublish_removes_route_before_stopping(monkeypatch, isolated_registry):
    item = PublishedService("docs", 3000, 52807, 52808, False, 99)
    published.save_services([item])
    events: list[str] = []
    monkeypatch.setattr(
        service_cli, "remove_route", lambda _item, **_kwargs: events.append("route")
    )
    monkeypatch.setattr(
        service_cli, "stop_gateway", lambda _item: events.append("gateway")
    )

    result = CliRunner().invoke(service_cli.service, ["unpublish", "docs"])

    assert result.exit_code == 0, result.output
    assert events == ["route", "gateway"]
    assert published.load_services() == []


def test_unpublish_compensates_serve_when_final_registry_save_fails(
    monkeypatch, isolated_registry
):
    item = PublishedService("docs", 3000, 52807, 52808, False, 99)
    published.save_services([item], last_applied_serve_hash="old-hash")
    real_save = published.save_registry
    save_calls = 0
    compensated = []

    def fail_final_save(registry):
        nonlocal save_calls
        save_calls += 1
        if save_calls == 2:
            raise OSError("forced final save failure")
        real_save(registry)

    monkeypatch.setattr(service_cli, "save_registry", fail_final_save)
    monkeypatch.setattr(
        service_cli, "remove_route", lambda _item, **_kwargs: "removed-hash"
    )
    monkeypatch.setattr(
        service_cli,
        "apply_route",
        lambda _item, **kwargs: compensated.append(kwargs) or "restored-hash",
    )
    monkeypatch.setattr(service_cli, "stop_gateway", lambda _item: None)

    result = CliRunner().invoke(service_cli.service, ["unpublish", "docs"])

    assert result.exit_code != 0
    assert "forced final save failure" in result.output
    assert compensated[0]["last_applied_hash"] == "removed-hash"
    assert published.load_registry() == published.ServiceRegistry((item,), "old-hash")


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
        routes.apply_route(PublishedService("docs", 3000, 52807, 52808))


def test_portless_path_mode_is_retired(isolated_registry):
    result = CliRunner().invoke(
        service_cli.service, ["publish", "docs", "3000", "--portless"]
    )

    assert result.exit_code != 0
    assert "No such option '--portless'" in result.output
    assert not isolated_registry.exists()


def test_hostname_provider_gate_runs_before_registry_write(
    monkeypatch, isolated_registry
):
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(
        service_cli,
        "allocate_private_service_hostname",
        lambda _name: (_ for _ in ()).throw(
            routes.HostnamePublicationUnavailable("unsupported provider")
        ),
    )

    result = CliRunner().invoke(
        service_cli.service,
        ["publish", "docs", "3000", "--mode", "hostname"],
    )

    assert result.exit_code != 0
    assert "unsupported provider" in result.output
    assert not isolated_registry.exists()


def _fake_dns_clock(monkeypatch):
    """Drive the DNS-propagation poll loop without real sleeping."""
    clock = {"now": 0.0}
    monkeypatch.setattr(routes.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(
        routes.time,
        "sleep",
        lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
    )
    return clock


def _stub_hostname_allocation(monkeypatch):
    """Provider and cloud stubs for a valid crm allocation on node 7."""
    provider = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    cloud = importlib.import_module("openbase_coder_cli.services.cloud_registration")
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(provider, "is_netmesh", lambda: True)
    monkeypatch.setattr(
        provider,
        "hostname_serve_capability",
        lambda: {"supported": True, "http_port": 80},
    )
    monkeypatch.setattr(
        provider,
        "status_json",
        lambda: {
            "Self": {
                "DNSName": "gabes-mac-mini-openbase.netmesh.openbase.cloud.",
                "TailscaleIPs": ["100.64.0.10"],
            }
        },
    )
    monkeypatch.setattr(
        cloud,
        "list_netmesh_devices",
        lambda: [
            {
                "id": "7",
                "given_name": "gabes-mac-mini-openbase",
                "ip_addresses": ["100.64.0.10"],
            }
        ],
    )
    monkeypatch.setattr(
        cloud,
        "allocate_netmesh_service_hostname",
        lambda **_kwargs: cloud.CloudReportResult(
            ok=True,
            supported=True,
            response={
                "hostname": "crm.gabes-mac-mini-openbase.netmesh.openbase.cloud",
                "node_id": "7",
                "service_name": "crm",
                "created": True,
            },
        ),
    )
    released = []
    monkeypatch.setattr(
        cloud,
        "release_netmesh_service_hostname",
        lambda **kwargs: released.append(kwargs)
        or cloud.CloudReportResult(ok=True, supported=True),
    )
    return released


def test_private_hostname_allocation_waits_for_dns_propagation(monkeypatch):
    released = _stub_hostname_allocation(monkeypatch)
    clock = _fake_dns_clock(monkeypatch)
    attempts = {"count": 0}

    def fake_getaddrinfo(*_args, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] < 6:
            raise routes.socket.gaierror("no such host")
        return [
            (
                routes.socket.AF_INET,
                routes.socket.SOCK_STREAM,
                6,
                "",
                ("100.64.0.10", 80),
            )
        ]

    monkeypatch.setattr(routes.socket, "getaddrinfo", fake_getaddrinfo)

    assert routes.allocate_private_service_hostname("crm") == (
        "crm.gabes-mac-mini-openbase.netmesh.openbase.cloud",
        "7",
        True,
    )
    assert attempts["count"] == 6
    assert clock["now"] == pytest.approx(10.0)
    assert released == []


def test_private_hostname_allocation_times_out_after_bounded_dns_wait(monkeypatch):
    released = _stub_hostname_allocation(monkeypatch)
    clock = _fake_dns_clock(monkeypatch)
    monkeypatch.setattr(
        routes.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            routes.socket.gaierror("no such host")
        ),
    )

    with pytest.raises(
        routes.HostnamePublicationUnavailable,
        match="did not resolve it within 30s",
    ):
        routes.allocate_private_service_hostname("crm")
    assert released == [{"node_id": "7", "service_name": "crm"}]
    assert clock["now"] <= routes.HOSTNAME_DNS_TIMEOUT_SECONDS


def test_private_hostname_allocation_must_resolve_to_this_node(monkeypatch):
    provider = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    cloud = importlib.import_module("openbase_coder_cli.services.cloud_registration")
    _fake_dns_clock(monkeypatch)
    monkeypatch.setattr(provider, "is_netmesh_tsnet", lambda: False)
    monkeypatch.setattr(provider, "is_netmesh", lambda: True)
    monkeypatch.setattr(
        provider,
        "hostname_serve_capability",
        lambda: {"supported": True, "http_port": 80},
    )
    monkeypatch.setattr(
        provider,
        "status_json",
        lambda: {
            "Self": {
                "DNSName": "gabes-mac-mini-openbase.netmesh.openbase.cloud.",
                "TailscaleIPs": ["100.64.0.10"],
            }
        },
    )
    monkeypatch.setattr(
        routes.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                routes.socket.AF_INET,
                routes.socket.SOCK_STREAM,
                6,
                "",
                ("100.64.0.10", 80),
            )
        ],
    )
    monkeypatch.setattr(
        cloud,
        "list_netmesh_devices",
        lambda: [
            {
                "id": "7",
                "given_name": "gabes-mac-mini-openbase",
                "ip_addresses": ["100.64.0.10"],
            }
        ],
    )
    monkeypatch.setattr(
        cloud,
        "allocate_netmesh_service_hostname",
        lambda **_kwargs: cloud.CloudReportResult(
            ok=True,
            supported=True,
            response={
                "hostname": "crm.gabes-mac-mini-openbase.netmesh.openbase.cloud",
                "node_id": "7",
                "service_name": "crm",
                "created": True,
            },
        ),
    )
    released = []
    monkeypatch.setattr(
        cloud,
        "release_netmesh_service_hostname",
        lambda **kwargs: released.append(kwargs)
        or cloud.CloudReportResult(ok=True, supported=True),
    )

    assert routes.allocate_private_service_hostname("crm") == (
        "crm.gabes-mac-mini-openbase.netmesh.openbase.cloud",
        "7",
        True,
    )

    monkeypatch.setattr(
        routes.socket,
        "getaddrinfo",
        lambda *_args, **_kwargs: [
            (
                routes.socket.AF_INET,
                routes.socket.SOCK_STREAM,
                6,
                "",
                ("100.64.0.99", 80),
            )
        ],
    )
    with pytest.raises(
        routes.HostnamePublicationUnavailable, match="did not resolve it to this node"
    ):
        routes.allocate_private_service_hostname("crm")
    assert released == [{"node_id": "7", "service_name": "crm"}]


def test_hostname_publish_uses_root_hostname_and_its_own_gateway(
    monkeypatch, isolated_registry
):
    applied = []
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(
        service_cli,
        "allocate_private_service_hostname",
        lambda _name: routes.ServiceHostnameAllocation(
            "docs.gabes-mac-mini-openbase.netmesh.openbase.cloud", "7", True
        ),
    )
    monkeypatch.setattr(service_cli, "allocate_hostname_proxy", lambda: 52808)
    monkeypatch.setattr(service_cli, "start_ephemeral_gateway", lambda _item: 99)
    monkeypatch.setattr(service_cli, "gateway_healthy", lambda _item: True)
    monkeypatch.setattr(
        service_cli,
        "apply_route",
        lambda item, **kwargs: applied.append((item, kwargs)) or "new-hash",
    )
    monkeypatch.setattr(
        service_cli,
        "service_url",
        lambda _item: "http://docs.gabes-mac-mini-openbase.netmesh.openbase.cloud/",
    )

    result = CliRunner().invoke(
        service_cli.service,
        ["publish", "docs", "3000", "--mode", "hostname", "--no-persist"],
    )

    assert result.exit_code == 0, result.output
    item = published.load_services()[0]
    assert item.mode == "hostname"
    assert item.tailnet_port == 80
    assert item.proxy_port == 52808
    assert item.hostname == "docs.gabes-mac-mini-openbase.netmesh.openbase.cloud"
    assert item.node_id == "7"
    assert published.load_registry().last_applied_serve_hash == "new-hash"
    assert (
        "http://docs.gabes-mac-mini-openbase.netmesh.openbase.cloud/" in result.output
    )
    assert applied[0][1]["previous_services"] == []


def test_hostname_publish_releases_new_dns_allocation_on_route_failure(
    monkeypatch, isolated_registry
):
    released = []
    monkeypatch.setattr(service_cli, "local_service_available", lambda _port: True)
    monkeypatch.setattr(service_cli, "allocate_hostname_proxy", lambda: 52808)
    monkeypatch.setattr(
        service_cli,
        "allocate_private_service_hostname",
        lambda _name: routes.ServiceHostnameAllocation(
            "docs.mac.netmesh.openbase.cloud", "7", True
        ),
    )
    monkeypatch.setattr(service_cli, "start_ephemeral_gateway", lambda _item: 99)
    monkeypatch.setattr(service_cli, "gateway_healthy", lambda _item: True)
    monkeypatch.setattr(
        service_cli,
        "apply_route",
        lambda _item, **_kwargs: (_ for _ in ()).throw(RuntimeError("route failed")),
    )
    monkeypatch.setattr(service_cli, "stop_gateway", lambda _item: None)
    monkeypatch.setattr(
        service_cli,
        "release_private_service_hostname",
        lambda name, node_id: released.append((name, node_id)),
    )

    result = CliRunner().invoke(
        service_cli.service,
        ["publish", "docs", "3000", "--mode", "hostname", "--no-persist"],
    )

    assert result.exit_code != 0
    assert "route failed" in result.output
    assert released == [("docs", "7")]
    assert published.load_services() == []


def test_hostname_unpublish_releases_dns_before_removing_route(
    monkeypatch, isolated_registry
):
    item = PublishedService(
        "docs",
        3000,
        80,
        52808,
        False,
        99,
        "hostname",
        "docs.mac.netmesh.openbase.cloud",
        "7",
    )
    published.save_services([item])
    events = []
    monkeypatch.setattr(
        service_cli,
        "release_private_service_hostname",
        lambda _name, _node_id: events.append("dns"),
    )
    monkeypatch.setattr(
        service_cli, "remove_route", lambda _item, **_kwargs: events.append("route")
    )
    monkeypatch.setattr(
        service_cli, "stop_gateway", lambda _item: events.append("gateway")
    )

    result = CliRunner().invoke(service_cli.service, ["unpublish", "docs"])

    assert result.exit_code == 0, result.output
    assert events == ["dns", "route", "gateway"]
    assert published.load_services() == []


def test_reconcile_rejects_unknown_drift_and_preserves_builtin_rules(monkeypatch):
    provider = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    applied = []
    monkeypatch.setattr(
        provider, "serve_snapshot", lambda: {"etag": "v1", "hash": "drift"}
    )
    monkeypatch.setattr(provider, "plan_serve", lambda _rules: {"hash": "expected"})
    monkeypatch.setattr(
        provider, "apply_serve", lambda rules, **kwargs: applied.append((rules, kwargs))
    )

    with pytest.raises(RuntimeError, match="drifted"):
        routes.reconcile_openbase_routes(
            [],
            [
                PublishedService(
                    "docs",
                    3000,
                    80,
                    52808,
                    mode="hostname",
                    hostname="docs.mac.netmesh.openbase.cloud",
                )
            ],
            None,
        )

    assert applied == []
    monkeypatch.setattr(
        provider, "serve_snapshot", lambda: {"etag": "v1", "hash": "expected"}
    )
    monkeypatch.setattr(
        provider,
        "apply_serve",
        lambda rules, **kwargs: applied.append((rules, kwargs)) or {"hash": "next"},
    )

    result = routes.reconcile_openbase_routes(
        [],
        [
            PublishedService(
                "docs",
                3000,
                80,
                52808,
                mode="hostname",
                hostname="docs.mac.netmesh.openbase.cloud",
            )
        ],
        None,
    )

    assert result == "next"
    assert applied[0][0][:2] == [
        {"kind": "openbase-console"},
        {"kind": "openbase-livekit"},
    ]
    assert applied[0][0][-1] == {
        "kind": "published-hostname",
        "hostname": "docs.mac.netmesh.openbase.cloud",
        "proxy_port": 52808,
    }


def test_reconcile_accepts_fresh_helper_empty_config_as_initial_base(monkeypatch):
    provider = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    applied = []
    monkeypatch.setattr(
        provider, "serve_snapshot", lambda: {"etag": "v1", "hash": "empty"}
    )
    monkeypatch.setattr(
        provider,
        "plan_serve",
        lambda rules: {"hash": "empty" if rules == [] else "baseline"},
    )
    monkeypatch.setattr(
        provider,
        "apply_serve",
        lambda rules, **kwargs: applied.append((rules, kwargs)) or {"hash": "next"},
    )

    result = routes.reconcile_openbase_routes(
        [],
        [
            PublishedService(
                "docs",
                3000,
                80,
                52808,
                mode="hostname",
                hostname="docs.mac.netmesh.openbase.cloud",
            )
        ],
        None,
    )

    assert result == "next"
    assert applied[0][1] == {"expected_etag": "v1", "expected_hash": "empty"}


def test_reconcile_keeps_drift_guard_once_a_hash_was_recorded(monkeypatch):
    provider = importlib.import_module("openbase_coder_cli.services.tailscale_provider")
    applied = []
    monkeypatch.setattr(
        provider, "serve_snapshot", lambda: {"etag": "v1", "hash": "empty"}
    )
    monkeypatch.setattr(
        provider,
        "plan_serve",
        lambda rules: {"hash": "empty" if rules == [] else "baseline"},
    )
    monkeypatch.setattr(
        provider, "apply_serve", lambda rules, **kwargs: applied.append((rules, kwargs))
    )

    with pytest.raises(RuntimeError, match="drifted"):
        routes.reconcile_openbase_routes([], [], "recorded")

    assert applied == []


@pytest.mark.parametrize("occupied_port", [80, 443])
def test_gateway_binds_loopback_on_its_uncommon_proxy_port(
    occupied_port, isolated_registry, monkeypatch
):
    published.save_services([PublishedService("docs", 3000, 52807, 52808)])
    invocation = {}
    monkeypatch.setattr(
        gateway.web,
        "run_app",
        lambda app, *, host, port: invocation.update(
            {"app": app, "host": host, "port": port}
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["service_gateway", "--name", "docs"],
    )

    gateway.main()

    assert occupied_port in {80, 443}
    assert invocation["host"] == "127.0.0.1"
    assert invocation["port"] == 52808
    assert invocation["port"] != occupied_port
