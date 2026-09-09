from __future__ import annotations

import asyncio
import hashlib
import ssl
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from aiohttp import (
    ClientConnectorCertificateError,
    ClientSession,
    TCPConnector,
    WSServerHandshakeError,
    web,
)
from test_service_https import LocalResolver, bundle
from test_service_https import service as https_service

from openbase_coder_cli.services import published_services as published
from openbase_coder_cli.services import service_diagnostics as diagnostics
from openbase_coder_cli.services import service_gateway as gateway
from openbase_coder_cli.services import service_https as https

# Register the shared fixture locally; test parameters are injected by pytest.
service_fixture = https_service


@asynccontextmanager
async def serving(service, monkeypatch, handler, *, days=90):
    upstream = web.Application(client_max_size=8 * 1024 * 1024)
    upstream.router.add_route("*", "/{path:.*}", handler)
    upstream_runner = web.AppRunner(upstream)
    await upstream_runner.setup()
    upstream_site = web.TCPSite(upstream_runner, "127.0.0.1", 0)
    await upstream_site.start()
    service = replace(
        service, local_port=upstream_site._server.sockets[0].getsockname()[1]
    )
    published.save_services([service])
    path, _ = bundle(service, days=days)
    monkeypatch.setattr(https, "is_active", lambda s: True)
    app = web.Application()
    app.cleanup_ctx.append(gateway.client_session)
    app.router.add_route("*", "/{path:.*}", https.dispatch)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=https.tls_context())
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield service, f"https://{service.hostname}:{port}", path, upstream_runner
    finally:
        await runner.cleanup()
        await upstream_runner.cleanup()


def client(path, service):
    return ClientSession(
        connector=TCPConnector(
            resolver=LocalResolver(), ssl=ssl.create_default_context(cafile=str(path))
        ),
        headers={"Host": service.hostname},
    )


@pytest.mark.asyncio
async def test_https_app_upload_stream_cookies_redirect_auth_and_websocket(
    service_fixture, monkeypatch
):
    service = service_fixture
    release_stream = asyncio.Event()

    async def application(request):
        if request.path == "/login":
            response = web.Response(text="signed in")
            response.set_cookie(
                "session", "test-session", secure=True, httponly=True, samesite="Lax"
            )
            return response
        if request.path == "/private":
            if request.cookies.get("session") != "test-session":
                raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
            return web.Response(text="private")
        if request.path == "/upload":
            return web.Response(text=hashlib.sha256(await request.read()).hexdigest())
        if request.path == "/redirect":
            raise web.HTTPFound("/private?next=1")
        if request.path == "/stream":
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b"data: first\n\n")
            await release_stream.wait()
            await response.write(b"data: last\n\n")
            return response
        if request.path == "/ws":
            if request.cookies.get("session") != "test-session":
                raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})
            ws = web.WebSocketResponse(protocols=["roofball-v1"])
            await ws.prepare(request)
            async for message in ws:
                await ws.send_str(message.data)
            return ws
        return web.Response(text=request.raw_path)

    async with serving(service, monkeypatch, application) as (item, url, path, _):
        async with client(path, item) as session:
            async with session.get(url + "/private") as response:
                assert response.status == 401
            with pytest.raises(WSServerHandshakeError) as denied:
                await session.ws_connect(url + "/ws")
            assert denied.value.status == 401
            assert denied.value.headers["WWW-Authenticate"] == "Bearer"
            async with session.get(url + "/login") as response:
                assert "Secure" in response.headers["Set-Cookie"]
                assert "HttpOnly" in response.headers["Set-Cookie"]
            async with session.get(url + "/private") as response:
                assert await response.text() == "private"
            payload = b"abcdefgh" * (256 * 1024)
            async with session.post(url + "/upload", data=payload) as response:
                assert await response.text() == hashlib.sha256(payload).hexdigest()
            async with session.get(
                url + "/redirect", allow_redirects=False
            ) as response:
                assert response.status == 302
                assert response.headers["Location"] == "/private?next=1"
            async with session.get(url + "/a%2Fb?x=one%20two") as response:
                assert await response.text() == "/a%2Fb?x=one%20two"
            async with session.get(url + "/stream") as response:
                assert (
                    await asyncio.wait_for(response.content.readexactly(13), 2)
                    == b"data: first\n\n"
                )
                release_stream.set()
                assert await response.read() == b"data: last\n\n"
            async with session.ws_connect(url + "/ws", protocols=["roofball-v1"]) as ws:
                assert ws.protocol == "roofball-v1"
                await ws.send_str("hello")
                assert await ws.receive_str(timeout=2) == "hello"
        # A second browser must not inherit the first browser's proxy-side cookie.
        async with client(path, item) as session:
            async with session.get(url + "/private") as response:
                assert response.status == 401


@pytest.mark.asyncio
async def test_tls_expiry_hot_replacement_unpublish_and_stopped_app(
    service_fixture, monkeypatch
):
    service = service_fixture

    async def application(request):
        return web.Response(text="ok")

    async with serving(service, monkeypatch, application, days=0) as (
        item,
        url,
        path,
        upstream,
    ):
        async with client(path, item) as session:
            with pytest.raises(ClientConnectorCertificateError):
                await session.get(url)
        # Replace the bundle without restarting the ingress; new handshakes reload it.
        bundle(item)
        async with client(path, item) as session:
            async with session.get(url) as response:
                assert await response.text() == "ok"
            await upstream.cleanup()
            async with session.get(url) as response:
                assert response.status == 502
                assert "Local application" in await response.text()
            published.save_services([])
            async with session.get(url) as response:
                assert response.status == 404


def test_diagnostics_distinguish_failure_layers(service_fixture, monkeypatch):
    service = service_fixture
    bundle(service)
    monkeypatch.setattr(
        diagnostics.provider,
        "status_json",
        lambda: {"BackendState": "Running", "Self": {"TailscaleIPs": ["100.64.0.1"]}},
    )
    monkeypatch.setattr(
        diagnostics.provider,
        "serve_status_json",
        lambda: {"TCP": {"443": {"TCPForward": "127.0.0.1:59443"}}},
    )
    monkeypatch.setattr(
        diagnostics.socket,
        "getaddrinfo",
        lambda *a, **k: [(None, None, None, None, ("100.64.0.1", 443))],
    )
    monkeypatch.setattr(published, "gateway_healthy", lambda *a, **k: True)
    monkeypatch.setattr(published, "local_service_available", lambda *a, **k: True)
    assert diagnostics.diagnose(service)["ready"]
    monkeypatch.setattr(published, "local_service_available", lambda *a, **k: False)
    result = diagnostics.diagnose(service)
    assert [c["check"] for c in result["checks"] if not c["ok"]] == ["application"]
    bundle(service, days=0)
    result = diagnostics.diagnose(service)
    assert "expired" in next(
        c["message"] for c in result["checks"] if c["check"] == "certificate"
    )


@pytest.mark.asyncio
async def test_https_worker_supervisor_retries_and_stops(service_fixture, monkeypatch):
    calls = []
    recovered = asyncio.Event()
    real_sleep = asyncio.sleep

    def ensure(service):
        calls.append(service)
        if len(calls) == 1:
            raise RuntimeError("temporary startup failure")

    async def next_tick(_delay):
        if len(calls) >= 2:
            recovered.set()
            await asyncio.Event().wait()
        await real_sleep(0)

    monkeypatch.setattr(https, "ensure_https_gateway", ensure)
    monkeypatch.setattr(gateway, "find_service", lambda name: service_fixture)
    monkeypatch.setattr(gateway.asyncio, "sleep", next_tick)
    app = web.Application()
    app[gateway.SERVICE_KEY] = service_fixture
    context = gateway.https_supervisor(app)
    await anext(context)
    await asyncio.wait_for(recovered.wait(), 2)
    await context.aclose()
    assert calls == [service_fixture, service_fixture]


def test_doctor_command_status_json_and_unknown_service(service_fixture, monkeypatch):
    import importlib
    import json

    from click.testing import CliRunner

    command = importlib.import_module("openbase_coder_cli.cli.service")
    published.save_services([service_fixture])
    monkeypatch.setattr(
        diagnostics,
        "diagnose",
        lambda s: {
            "ready": False,
            "checks": [
                {
                    "check": "dns",
                    "ok": False,
                    "message": "Private hostname DNS lookup failed",
                }
            ],
        },
    )
    result = CliRunner().invoke(
        command.service, ["doctor", service_fixture.name, "--json"]
    )
    assert result.exit_code == 1
    assert json.loads(result.output)["checks"][0]["check"] == "dns"
    result = CliRunner().invoke(command.service, ["doctor", "absent"])
    assert result.exit_code == 1
    assert "not published" in result.output
