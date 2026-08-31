from __future__ import annotations

import argparse
import asyncio
from collections.abc import AsyncIterator

from aiohttp import ClientSession, WSMsgType, web

from openbase_coder_cli.services.published_services import (
    HEALTH_PATH,
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
SERVICE_KEY = web.AppKey("service", PublishedService)
CLIENT_KEY = web.AppKey("client", ClientSession)


def upstream_path(path_qs: str, name: str) -> str:
    prefix = f"/{name}"
    if path_qs == prefix:
        return "/"
    if path_qs.startswith(f"{prefix}/"):
        return path_qs[len(prefix) :]
    return path_qs


def _forward_headers(headers) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower() not in HOP_BY_HOP_HEADERS and key.lower() != "host"
    }


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


async def _proxy_websocket(request: web.Request, upstream: str) -> web.StreamResponse:
    downstream = web.WebSocketResponse()
    await downstream.prepare(request)
    session = request.app[CLIENT_KEY]
    async with session.ws_connect(
        upstream, headers=_forward_headers(request.headers)
    ) as source:
        await asyncio.gather(
            _relay_websocket(downstream, source),
            _relay_websocket(source, downstream),
        )
    return downstream


async def proxy(request: web.Request) -> web.StreamResponse:
    service = request.app[SERVICE_KEY]
    if request.path == HEALTH_PATH:
        return web.json_response({"ok": True, "service": service.name})
    path = upstream_path(request.path_qs, service.name)
    upstream = f"http://127.0.0.1:{service.local_port}{path}"
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_websocket(request, upstream.replace("http://", "ws://", 1))
    session = request.app[CLIENT_KEY]
    body = await request.read()
    async with session.request(
        request.method,
        upstream,
        headers=_forward_headers(request.headers),
        data=body,
        allow_redirects=False,
    ) as response:
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in HOP_BY_HOP_HEADERS
        }
        return web.Response(
            body=await response.read(), status=response.status, headers=headers
        )


async def client_session(_app: web.Application) -> AsyncIterator[None]:
    async with ClientSession() as session:
        _app[CLIENT_KEY] = session
        yield


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
    service = find_service(args.name)
    if service is None:
        raise SystemExit(f"Published service '{args.name}' no longer exists.")
    web.run_app(create_app(service.name), host="127.0.0.1", port=service.proxy_port)


if __name__ == "__main__":
    main()
