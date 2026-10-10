"""Real TCP regression tests: exact browser bytes, auth, ownership and expiry."""

import asyncio
import secrets
from dataclasses import replace

import pytest

from openbase_coder_cli.callback_relay import PROTOCOL, CallbackRelay, ListenerOwner


async def fixture(ttl=2):
    received = []

    async def callback(reader, writer):
        received.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\nConnection: close\r\n\r\nsigned-in"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(callback, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    # On macOS global net_connections requires root. Process-local inspection
    # is sufficient to identify this test-owned listener without privileges.
    import os

    import psutil

    proc = psutil.Process(os.getpid())
    conn = next(c for c in proc.net_connections(kind="tcp") if c.laddr.port == port)
    owner = ListenerOwner(proc.pid, proc.create_time(), conn.fd, "127.0.0.1", port)
    token = secrets.token_urlsafe(24)
    relay = CallbackRelay(owner, token, ttl)
    relay_port = await relay.start()
    run = asyncio.create_task(relay.run())
    return server, relay, relay_port, run, received


@pytest.mark.asyncio
async def test_callback_contract_auth_preconnect_and_owner_cleanup():
    server, relay, port, run, received = await fixture()
    try:
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(b"OPENBASE-LOOPBACK/1 wrong\n")
        await w.drain()
        assert await r.read() == b""
        w.close()
        assert received == [] and not relay.done.is_set()
        # Setup health probe authenticates but must not consume callback.
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"{PROTOCOL} {relay.token}\n".encode())
        await w.drain()
        assert await r.readline() == b"OK\n"
        w.close()
        await w.wait_closed()
        assert received == []
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"{PROTOCOL} {relay.token}\n".encode())
        await w.drain()
        assert await r.readline() == b"OK\n"
        request = f"GET /callback?code=synthetic&state=fixture HTTP/1.1\r\nHost: localhost:{relay.owner.port}\r\n\r\n".encode()
        w.write(request)
        await w.drain()
        response = await asyncio.wait_for(r.read(), 2)
        assert response.endswith(b"signed-in")
        assert received == [request]
        server.close()
        await server.wait_closed()
        await asyncio.wait_for(run, 2)
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
        w.close()
    finally:
        relay.done.set()
        await run
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_expiry_closes_idle_authenticated_connections():
    server, relay, port, run, received = await fixture(ttl=0.15)
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"{PROTOCOL} {relay.token}\n".encode())
    await w.drain()
    assert await r.readline() == b"OK\n"
    await asyncio.wait_for(run, 2)
    assert await r.read() == b"" and not received
    w.close()
    server.close()
    await server.wait_closed()


@pytest.mark.asyncio
async def test_changed_process_identity_cannot_receive_callback():
    server, relay, port, run, received = await fixture()
    relay.owner = replace(relay.owner, created=0)
    await asyncio.wait_for(run, 2)
    assert not received
    with pytest.raises(OSError):
        await asyncio.open_connection("127.0.0.1", port)
    server.close()
    await server.wait_closed()


def test_find_refuses_wildcard_and_foreign_owner(monkeypatch):
    from types import SimpleNamespace as N

    import psutil

    monkeypatch.setattr(
        psutil,
        "net_connections",
        lambda **kw: [
            N(status=psutil.CONN_LISTEN, laddr=N(port=8085, ip="0.0.0.0"), pid=1)
        ],
    )
    with pytest.raises(ValueError, match="loopback-only"):
        ListenerOwner.find(8085)
