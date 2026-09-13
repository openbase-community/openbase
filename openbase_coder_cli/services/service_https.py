"""Shared loopback TLS ingress behind VPN-only TCP Serve, never a public listener."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import time
import weakref
from dataclasses import replace

from aiohttp import web

from openbase_coder_cli.services import published_services as published
from openbase_coder_cli.services.service_certificates import (
    certificate_directory,
    ensure_certificate,
    private_lock,
    private_write,
)
from openbase_coder_cli.services.service_gateway import client_session, proxy_service

logger = logging.getLogger(__name__)
SNI_NAMES = weakref.WeakKeyDictionary()


def _state_path():
    return published._registry_path().with_name("published-https.json")


def _worker(service):
    return replace(
        service, name="_https", proxy_port=published.HTTPS_PROXY_PORT, persistent=False
    )


def ensure_https_gateway(service):
    path = _state_path()
    with private_lock(path.with_suffix(".lock")):
        pid = json.loads(path.read_text()).get("pid") if path.exists() else None
        worker = replace(_worker(service), pid=pid)
        if (
            pid
            and published._pid_is_gateway(worker)
            and published.local_service_available(worker.proxy_port)
        ):
            return
        if published.local_service_available(worker.proxy_port):
            raise RuntimeError(
                "The private HTTPS ingress port is occupied by another process."
            )
        pid = published.start_ephemeral_gateway(worker)
        private_write(path, json.dumps({"pid": pid}).encode())
        if not published.gateway_healthy(worker, timeout=10):
            published.stop_gateway(replace(worker, pid=pid))
            raise RuntimeError("The private HTTPS ingress could not start.")


def https_services():
    return [
        s
        for s in published.load_registry().services
        if s.mode == published.MODE_HOSTNAME and s.tailnet_port == 443
    ]


def is_active(service):
    # Session entries remain on disk after logout. They must not be reactivated
    # when an explicitly persistent, unrelated publication starts at login.
    return (
        service.pid is None or published._pid_is_gateway(service)
    ) and published.local_service_available(service.proxy_port, timeout=0.1)


def tls_context():
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    contexts = {}

    def server_name(connection, name, _initial):
        service = next((s for s in https_services() if s.hostname == name), None)
        if service is None:
            return ssl.ALERT_DESCRIPTION_UNRECOGNIZED_NAME
        bundle = certificate_directory(service) / "certificate.pem"
        signature = (str(bundle), bundle.stat().st_mtime_ns)
        if signature not in contexts:
            updated = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            updated.minimum_version = ssl.TLSVersion.TLSv1_2
            updated.load_cert_chain(bundle)
            contexts.clear()
            contexts[signature] = updated
        connection.context = contexts[signature]
        SNI_NAMES[connection] = name

    context.sni_callback = server_name
    return context


async def dispatch(request):
    name = request.host.lower().removesuffix(":443")
    tls = request.transport.get_extra_info("ssl_object") if request.transport else None
    if not request.secure or SNI_NAMES.get(tls) != name:
        raise web.HTTPMisdirectedRequest()
    service = next((s for s in https_services() if s.hostname == name), None)
    if service is None or not await asyncio.to_thread(is_active, service):
        raise web.HTTPNotFound()
    return await proxy_service(request, service)


async def maintain(app):
    async def run():
        last_active = time.monotonic()
        next_renewal = 0
        while True:
            services = [
                s for s in https_services() if await asyncio.to_thread(is_active, s)
            ]
            if services:
                last_active = time.monotonic()
                if time.monotonic() >= next_renewal:
                    next_renewal = time.monotonic() + 3600
                    for service in {
                        s.hostname.split(".", 1)[1]: s for s in services
                    }.values():
                        try:
                            await asyncio.to_thread(ensure_certificate, service)
                        except Exception:
                            # Keep the existing certificate and active streams;
                            # retry later, rather than interrupting a valid service.
                            next_renewal = time.monotonic() + 300
                            logger.error(
                                "Private HTTPS renewal failed; retaining existing certificate and retrying in five minutes."
                            )
            elif time.monotonic() - last_active > 60:
                raise web.GracefulExit()
            await asyncio.sleep(10)

    task = asyncio.create_task(run())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def main():
    app = web.Application()
    app.cleanup_ctx.extend([client_session, maintain])
    app.router.add_route("*", "/{path:.*}", dispatch)
    web.run_app(
        app,
        host="127.0.0.1",
        port=published.HTTPS_PROXY_PORT,
        ssl_context=tls_context(),
    )
