from __future__ import annotations

import argparse
import asyncio
import logging
from collections.abc import AsyncIterator, Iterable

from aiohttp import (
    ClientConnectionError,
    ClientPayloadError,
    ClientSession,
    ClientTimeout,
    DummyCookieJar,
    WSMsgType,
    WSServerHandshakeError,
    web,
)
from multidict import CIMultiDict
from yarl import URL

from openbase_coder_cli.services.published_services import (
    PublishedService,
    find_service,
)

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}
UNTRUSTED_FORWARDED_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-port",
    "x-forwarded-prefix",
    "x-forwarded-proto",
}
SERVICE_KEY = web.AppKey("service", PublishedService)
CLIENT_KEY = web.AppKey("client", ClientSession)
logger = logging.getLogger(__name__)


def _connection_headers(headers: Iterable[tuple[str, str]]) -> set[str]:
    nominated: set[str] = set()
    for key, value in headers:
        if key.lower() == "connection":
            nominated.update(part.strip().lower() for part in value.split(","))
    return nominated


def _filtered_headers(
    headers: Iterable[tuple[str, str]], *, strip_forwarded: bool = False
) -> CIMultiDict[str]:
    items = list(headers)
    blocked = HOP_BY_HOP_HEADERS | _connection_headers(items)
    if strip_forwarded:
        blocked |= UNTRUSTED_FORWARDED_HEADERS
    result: CIMultiDict[str] = CIMultiDict()
    for key, value in items:
        if key.lower() not in blocked and key.lower() != "host":
            result.add(key, value)
    return result


def _forward_headers(
    request: web.Request, service: PublishedService
) -> CIMultiDict[str]:
    headers = _filtered_headers(request.headers.items(), strip_forwarded=True)
    if request.remote:
        headers["X-Forwarded-For"] = request.remote
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = request.scheme
    headers["X-Forwarded-Port"] = str(service.tailnet_port)
    return headers


async def _relay_websocket(source, destination) -> None:
    async for message in source:
        if message.type == WSMsgType.TEXT:
            await destination.send_str(message.data)
        elif message.type == WSMsgType.BINARY:
            await destination.send_bytes(message.data)
        elif message.type == WSMsgType.PING:
            await destination.ping(message.data)
        elif message.type == WSMsgType.PONG:
            await destination.pong(message.data)
        elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
            break


async def _proxy_websocket(
    request: web.Request, upstream: str, service: PublishedService
) -> web.StreamResponse:
    session = request.app[CLIENT_KEY]
    protocols = [
        p.strip()
        for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if p.strip()
    ]
    try:
        async with asyncio.timeout(10):
            source = await session.ws_connect(
                URL(upstream, encoded=True),
                headers=_forward_headers(request, service),
                protocols=protocols,
            )
    except WSServerHandshakeError as exc:
        headers = _filtered_headers((exc.headers or {}).items())
        # The handshake exception carries headers, not the upstream body.
        headers.popall("Content-Length", None)
        headers.popall("Content-Encoding", None)
        return web.Response(status=exc.status, headers=headers)
    async with source:
        downstream = web.WebSocketResponse(
            protocols=[source.protocol] if source.protocol else []
        )
        for value in source._response.headers.getall("Set-Cookie", []):
            downstream.headers.add("Set-Cookie", value)
        await downstream.prepare(request)
        tasks = {
            asyncio.create_task(_relay_websocket(downstream, source)),
            asyncio.create_task(_relay_websocket(source, downstream)),
        }
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await downstream.close()
    return downstream


async def proxy(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    return await proxy_service(request, service)


async def proxy_service(
    request: web.Request, service: PublishedService
) -> web.StreamResponse:
    try:
        return await _proxy_service(request, service)
    except (ClientConnectionError, TimeoutError) as exc:
        raise web.HTTPBadGateway(
            text="Local application is unavailable or its connection failed."
        ) from exc


async def _proxy_service(
    request: web.Request, service: PublishedService
) -> web.StreamResponse:
    if (
        service.mode == "hostname"
        and request.host.lower().removesuffix(f":{443 if request.secure else 80}")
        != service.hostname
    ):
        # Serve's internal lookup key is not an alternate public Host name.
        raise web.HTTPNotFound()
    if service.tailnet_port == 443 and not request.secure:
        raise web.HTTPPermanentRedirect(f"https://{service.hostname}{request.raw_path}")
    # Every publication owns a root ingress. Service names are not URL prefixes.
    upstream = f"http://127.0.0.1:{service.local_port}{request.raw_path}"
    if request.headers.get("Upgrade", "").lower() == "websocket":
        # The externally visible tailnet leg is WireGuard-encrypted. This
        # WebSocket hop is hard-coded to loopback and never leaves this machine.
        websocket_upstream = upstream.replace(
            "http://",
            "ws://",  # nosemgrep: detect-insecure-websocket
            1,
        )
        return await _proxy_websocket(request, websocket_upstream, service)

    session = request.app[CLIENT_KEY]
    async with session.request(
        request.method,
        URL(upstream, encoded=True),
        headers=_forward_headers(request, service),
        data=request.content.iter_any(),
        allow_redirects=False,
    ) as response:
        downstream = web.StreamResponse(status=response.status, reason=response.reason)
        for key, value in _filtered_headers(response.headers.items()).items():
            downstream.headers.add(key, value)
        await downstream.prepare(request)
        try:
            async for chunk in response.content.iter_any():
                await downstream.write(chunk)
        except (ClientConnectionError, ClientPayloadError, TimeoutError):
            # Headers are already sent: a new 502 would corrupt the response.
            # End the connection so the caller observes an incomplete stream.
            downstream.force_close()
            if request.transport:
                request.transport.close()
            return downstream
        await downstream.write_eof()
        return downstream


async def client_session(app: web.Application) -> AsyncIterator[None]:
    # Proxy cookies belong to the caller, never a shared server-side cookie jar.
    # Long-running streams have no total deadline; connecting is still bounded.
    async with ClientSession(
        auto_decompress=False,
        cookie_jar=DummyCookieJar(),
        timeout=ClientTimeout(total=None, sock_connect=5),
    ) as session:
        app[CLIENT_KEY] = session
        yield


async def https_supervisor(app: web.Application) -> AsyncIterator[None]:
    """Restore a crashed TLS worker while this explicitly published gateway lives."""
    from openbase_coder_cli.services.service_https import ensure_https_gateway

    async def supervise():
        failed = False
        while True:
            current = find_service(app[SERVICE_KEY].name)
            if current is None or current.proxy_port != app[SERVICE_KEY].proxy_port:
                raise web.GracefulExit()
            try:
                await asyncio.to_thread(ensure_https_gateway, app[SERVICE_KEY])
                failed = False
            except (OSError, ValueError, RuntimeError):
                if not failed:
                    logger.error(
                        "HTTPS ingress unavailable; retrying recovery in five seconds. Run service doctor for diagnostics."
                    )
                failed = True
            await asyncio.sleep(5)

    task = asyncio.create_task(supervise())
    try:
        yield
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def create_app(name: str) -> web.Application:
    service = find_service(name)
    if service is None:
        raise RuntimeError(f"Published service '{name}' no longer exists.")
    app = web.Application()
    app[SERVICE_KEY] = service
    app.cleanup_ctx.append(client_session)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Openbase tailnet service gateway")
    parser.add_argument("--name", required=True)
    args = parser.parse_args()
    if args.name == "_https":
        from openbase_coder_cli.services.service_https import main as https_main

        https_main()
        return
    service = find_service(args.name)
    if service is None:
        raise SystemExit(f"Published service '{args.name}' no longer exists.")
    if service.tailnet_port == 443:
        from openbase_coder_cli.services.service_https import ensure_https_gateway

        ensure_https_gateway(service)
    app = create_app(service.name)
    if service.tailnet_port == 443:
        app.cleanup_ctx.append(https_supervisor)
    web.run_app(app, host="127.0.0.1", port=service.proxy_port)


if __name__ == "__main__":
    main()
