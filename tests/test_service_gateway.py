from __future__ import annotations

import importlib

import pytest
from aiohttp import ClientSession, WSMsgType, web
from yarl import URL

from openbase_coder_cli.services.published_services import PublishedService

published = importlib.import_module("openbase_coder_cli.services.published_services")
gateway = importlib.import_module("openbase_coder_cli.services.service_gateway")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["hostname", "dynamic"])
async def test_gateway_preserves_raw_paths_and_queries(isolated_registry, mode):
    async def echo(request):
        return web.Response(text=request.raw_path)

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
                80 if mode == "hostname" else 52807,
                52808,
                mode=mode,
                hostname="docs.abcd2345efgh.vpn.example.test"
                if mode == "hostname"
                else None,
                node_id="7" if mode == "hostname" else None,
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
            for path in (
                "/",
                "/docs",
                "/docs/",
                "/docs?next=/api",
                "/docs/api?q=1",
                "/services/docs/api",
                "/assets/app.js",
                "/docs/a%2Fb?q=%2f&same=1&same=2&empty=",
            ):
                response = await client.get(
                    URL(f"http://127.0.0.1:{proxy_port}{path}", encoded=True),
                    headers={
                        "Host": "docs.abcd2345efgh.vpn.example.test"
                    },
                )
                assert response.status == 200
                assert await response.text() == path
            if mode == "hostname":
                for host in (
                    "foreign.example.test",
                    "docs.abcd2345efgh.vpn.example.test.net.example.test",
                ):
                    response = await client.get(
                        f"http://127.0.0.1:{proxy_port}/", headers={"Host": host}
                    )
                    assert response.status == 404
    finally:
        await proxy_runner.cleanup()
        await backend_runner.cleanup()


@pytest.mark.asyncio
async def test_dynamic_gateway_does_not_alias_service_name_to_root(isolated_registry):
    async def root(_request):
        return web.Response(text="root")

    backend = web.Application()
    backend.router.add_get("/", root)
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
            root_response = await client.get(f"http://127.0.0.1:{proxy_port}/")
            assert await root_response.text() == "root"
            for path in ("/docs", "/docs/", "/services/docs/"):
                response = await client.get(
                    f"http://127.0.0.1:{proxy_port}{path}", allow_redirects=False
                )
                assert response.status == 404
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

    async def redirect(_request):
        return web.Response(
            status=302,
            headers={
                "Location": "/docs/login?next=%2F",
                "Set-Cookie": "auth=1; Path=/docs",
            },
        )

    backend = web.Application()
    backend.router.add_post("/upload", upload)
    backend.router.add_get("/redirect", redirect)
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
            redirected = await client.get(
                f"http://127.0.0.1:{proxy_port}/redirect", allow_redirects=False
            )
            assert redirected.status == 302
            assert redirected.headers["Location"] == "/docs/login?next=%2F"
            assert redirected.headers["Set-Cookie"] == "auth=1; Path=/docs"
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
@pytest.mark.parametrize("mode", ["hostname", "dynamic"])
@pytest.mark.parametrize("path", ["/socket", "/chat/socket?q=%2f"])
async def test_gateway_forwards_websockets_without_rewriting_paths(
    isolated_registry, mode, path
):
    async def websocket(request):
        response = web.WebSocketResponse()
        await response.prepare(request)
        async for message in response:
            if message.type == WSMsgType.TEXT:
                await response.send_str(f"{request.raw_path}:echo:{message.data}")
        return response

    backend = web.Application()
    backend.router.add_get("/{path:.*}", websocket)
    backend_runner = web.AppRunner(backend)
    await backend_runner.setup()
    backend_site = web.TCPSite(backend_runner, "127.0.0.1", 0)
    await backend_site.start()
    backend_port = backend_site._server.sockets[0].getsockname()[1]
    published.save_services(
        [
            PublishedService(
                "chat",
                backend_port,
                80 if mode == "hostname" else 52807,
                52808,
                mode=mode,
                hostname="chat.mac.net.obs.so" if mode == "hostname" else None,
                node_id="7" if mode == "hostname" else None,
            )
        ]
    )
    proxy_runner = web.AppRunner(gateway.create_app("chat"))
    await proxy_runner.setup()
    proxy_site = web.TCPSite(proxy_runner, "127.0.0.1", 0)
    await proxy_site.start()
    proxy_port = proxy_site._server.sockets[0].getsockname()[1]
    try:
        async with ClientSession() as client:
            async with client.ws_connect(
                URL(f"http://127.0.0.1:{proxy_port}{path}", encoded=True),
                headers={"Host": "chat.mac.net.obs.so"},
            ) as connection:
                await connection.send_str("hello")
                message = await connection.receive(timeout=2)
                assert message.data == f"{path}:echo:hello"
    finally:
        await proxy_runner.cleanup()
        await backend_runner.cleanup()
