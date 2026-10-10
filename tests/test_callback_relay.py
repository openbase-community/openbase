"""Real TCP regression tests: exact browser bytes, auth, ownership and expiry."""

import asyncio
import secrets
from dataclasses import replace

import pytest

from openbase_coder_cli.callback_relay import PROTOCOL, CallbackRelay, ListenerOwner


async def fixture(ttl=2, *, monitor=True):
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
    run = asyncio.create_task(relay.run()) if monitor else None
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


@pytest.mark.asyncio
async def test_expired_relay_rejects_traffic_before_monitor_starts():
    server, relay, port, run, received = await fixture(monitor=False)
    try:
        relay.deadline = 0
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        assert await reader.read() == b""
        writer.close()
        assert not received
    finally:
        relay.done.set()
        await relay.run()
        server.close()
        await server.wait_closed()


def test_listener_fd_reuse_does_not_preserve_identity(monkeypatch):
    from types import SimpleNamespace

    import psutil

    process = SimpleNamespace(
        create_time=lambda: 10,
        net_connections=lambda **kwargs: [
            SimpleNamespace(
                fd=5,
                status=psutil.CONN_LISTEN,
                laddr=SimpleNamespace(ip="127.0.0.1", port=8085),
            )
        ],
    )
    monkeypatch.setattr(psutil, "Process", lambda pid: process)
    monkeypatch.setattr(
        ListenerOwner, "identity", staticmethod(lambda pid, fd: "socket:[2]")
    )
    owner = ListenerOwner(123, 10, 5, "127.0.0.1", 8085, "socket:[1]")
    assert not owner.alive()


@pytest.mark.asyncio
async def test_binary_half_closed_requests_survive_multiple_exchanges():
    import os

    import psutil

    payload = bytes(range(256)) * 1024

    async def callback(reader, writer):
        data = await reader.read()
        writer.write(data[::-1])
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(callback, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    process = psutil.Process(os.getpid())
    connection = next(
        item for item in process.net_connections(kind="tcp") if item.laddr.port == port
    )
    relay = CallbackRelay(
        ListenerOwner(
            process.pid, process.create_time(), connection.fd, "127.0.0.1", port
        ),
        "synthetic",
        3,
    )
    relay_port = await relay.start()
    run = asyncio.create_task(relay.run())
    try:
        for _ in range(2):
            reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
            writer.write(f"{PROTOCOL} {relay.token}\n".encode())
            await writer.drain()
            assert await reader.readline() == b"OK\n"
            writer.write(payload)
            await writer.drain()
            writer.write_eof()
            assert await asyncio.wait_for(reader.read(), 2) == payload[::-1]
            writer.close()
    finally:
        relay.done.set()
        await run
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_callback_can_close_listener_on_accept_and_finish_response():
    import os

    import psutil

    received = []

    async def callback(reader, writer):
        server.close()
        received.append(await reader.readuntil(b"\r\n\r\n"))
        await asyncio.sleep(0.7)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(callback, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    process = psutil.Process(os.getpid())
    connection = next(
        item for item in process.net_connections(kind="tcp") if item.laddr.port == port
    )
    relay = CallbackRelay(
        ListenerOwner(
            process.pid, process.create_time(), connection.fd, "127.0.0.1", port
        ),
        "synthetic",
        2,
    )
    relay_port = await relay.start()
    run = asyncio.create_task(relay.run())
    reader, writer = await asyncio.open_connection("127.0.0.1", relay_port)
    try:
        writer.write(f"{PROTOCOL} {relay.token}\n".encode())
        await writer.drain()
        assert await reader.readline() == b"OK\n"
        writer.write(b"GET /callback HTTP/1.1\r\n\r\n")
        await writer.drain()
        assert (await asyncio.wait_for(reader.read(), 2)).endswith(b"\r\n\r\nOK")
        assert received == [b"GET /callback HTTP/1.1\r\n\r\n"]
    finally:
        writer.close()
        relay.done.set()
        await run
        server.close()
        await server.wait_closed()
