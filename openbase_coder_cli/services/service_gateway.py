from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator, Iterable
from urllib.parse import unquote, urlsplit

from aiohttp import ClientSession, WSMsgType, web
from multidict import CIMultiDict

from openbase_coder_cli.services.published_services import (
    HEALTH_PATH,
    MODE_PORTLESS,
    PORTLESS_PATH_PREFIX,
    PublishedService,
    find_service,
    load_services,
    validate_name,
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
DISPATCHER_KEY = web.AppKey("dispatcher", bool)
CLIENT_KEY = web.AppKey("client", ClientSession)


def upstream_path(path_qs: str, name: str, *, dispatcher: bool = False) -> str:
    prefix = f"/{name}"
    if dispatcher and path_qs.startswith(PORTLESS_PATH_PREFIX):
        prefix = f"{PORTLESS_PATH_PREFIX}{name}"
    if path_qs == prefix:
        return "/"
    if path_qs.startswith(f"{prefix}/"):
        return path_qs[len(prefix) :]
    if dispatcher:
        raise web.HTTPNotFound()
    return path_qs


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
    headers["X-Forwarded-Proto"] = "http"
    headers["X-Forwarded-Port"] = str(service.tailnet_port)
    if service.mode == MODE_PORTLESS:
        headers["X-Forwarded-Prefix"] = f"{PORTLESS_PATH_PREFIX}{service.name}"
    return headers


def _raw_segments(raw_path: str) -> list[str]:
    path = urlsplit(raw_path).path
    if "\\" in path or "%2f" in path.lower() or "%5c" in path.lower():
        raise web.HTTPBadRequest(text="Encoded path separators are not allowed.")
    segments = path.split("/")
    decoded = []
    for segment in segments:
        value = segment
        for _attempt in range(3):
            expanded = unquote(value)
            if expanded in {".", ".."}:
                raise web.HTTPBadRequest(text="Path traversal is not allowed.")
            if "/" in expanded or "\\" in expanded:
                raise web.HTTPBadRequest(
                    text="Encoded path separators are not allowed."
                )
            if expanded == value:
                break
            value = expanded
        decoded.append(value)
    return decoded


def dispatcher_service(raw_path_qs: str) -> PublishedService:
    segments = _raw_segments(raw_path_qs)
    offset = 1
    if len(segments) > 1 and segments[1] == "services":
        offset = 2
    if len(segments) <= offset:
        raise web.HTTPNotFound()
    try:
        name = validate_name(segments[offset])
    except ValueError as exc:
        raise web.HTTPNotFound() from exc
    service = next(
        (
            item
            for item in load_services()
            if item.name == name and item.mode == MODE_PORTLESS
        ),
        None,
    )
    if service is None:
        raise web.HTTPNotFound()
    return service


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
    downstream = web.WebSocketResponse()
    await downstream.prepare(request)
    session = request.app[CLIENT_KEY]
    async with session.ws_connect(
        upstream, headers=_forward_headers(request, service)
    ) as source:
        tasks = {
            asyncio.create_task(_relay_websocket(downstream, source)),
            asyncio.create_task(_relay_websocket(source, downstream)),
        }
        _done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    return downstream


async def proxy(request: web.Request) -> web.StreamResponse:
    if request.path == HEALTH_PATH:
        return web.json_response({"ok": True, "gateway": "openbase-service-dispatcher"})
    dispatcher = request.app[DISPATCHER_KEY]
    service = (
        dispatcher_service(request.raw_path) if dispatcher else request.app[SERVICE_KEY]
    )
    path = upstream_path(request.path_qs, service.name, dispatcher=dispatcher)
    upstream = f"http://127.0.0.1:{service.local_port}{path}"
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
        upstream,
        headers=_forward_headers(request, service),
        data=request.content.iter_any(),
        allow_redirects=False,
    ) as response:
        downstream = web.StreamResponse(status=response.status, reason=response.reason)
        for key, value in _filtered_headers(response.headers.items()).items():
            downstream.headers.add(key, value)
        await downstream.prepare(request)
        async for chunk in response.content.iter_any():
            await downstream.write(chunk)
        await downstream.write_eof()
        return downstream


async def client_session(app: web.Application) -> AsyncIterator[None]:
    async with ClientSession(auto_decompress=False) as session:
        app[CLIENT_KEY] = session
        yield


def create_app(name: str | None = None, *, dispatcher: bool = False) -> web.Application:
    if dispatcher == (name is not None):
        raise ValueError("Select exactly one named gateway or the shared dispatcher.")
    service = None if dispatcher else find_service(str(name))
    if not dispatcher and service is None:
        raise RuntimeError(f"Published service '{name}' no longer exists.")
    app = web.Application()
    app[DISPATCHER_KEY] = dispatcher
    if service is not None:
        app[SERVICE_KEY] = service
    app.cleanup_ctx.append(client_session)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Openbase tailnet service gateway")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--name")
    mode.add_argument("--dispatcher", action="store_true")
    parser.add_argument("--port", type=int)
    args = parser.parse_args()
    if args.dispatcher:
        services = [item for item in load_services() if item.mode == MODE_PORTLESS]
        if not services:
            raise SystemExit("No portless published services exist.")
        port = args.port or services[0].proxy_port
        if any(item.proxy_port != port for item in services):
            raise SystemExit("Portless registry entries disagree on dispatcher port.")
        web.run_app(create_app(dispatcher=True), host="127.0.0.1", port=port)
        return
    service = find_service(args.name)
    if service is None:
        raise SystemExit(f"Published service '{args.name}' no longer exists.")
    web.run_app(create_app(service.name), host="127.0.0.1", port=service.proxy_port)


if __name__ == "__main__":
    main()
