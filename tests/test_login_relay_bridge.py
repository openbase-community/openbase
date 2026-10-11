"""The phone reaches a host-VPN login relay as a WebSocket through the local API.

Real TCP and a real relay: the bridge passes the relay protocol through
unchanged, and dials only a relay registered right now.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import time

import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from asgiref.testing import ApplicationCommunicator  # noqa: E402

django.setup()

from openbase_coder_cli import login_relay_registry  # noqa: E402
from openbase_coder_cli.callback_relay import (  # noqa: E402
    PROTOCOL,
    CallbackRelay,
    ListenerOwner,
)
from openbase_coder_cli.login_callback import relay_capability  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import (  # noqa: E402
    login_relay_consumer,
    routing,
)


@pytest.fixture(autouse=True)
def registry(monkeypatch, tmp_path):
    monkeypatch.setattr(login_relay_registry, "REGISTRY_DIR", tmp_path / "relays")


async def _relay(ttl=5):
    """A callback server plus a relay on a non-loopback address ([::1] here)."""

    async def callback(reader, writer):
        await reader.readuntil(b"\r\n\r\n")
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nConnection: close\r\n\r\nsigned-in"
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(callback, "127.0.0.1", 0)
    owner = ListenerOwner.find(server.sockets[0].getsockname()[1])
    relay = CallbackRelay(owner, "", ttl, bind_host="::1")
    port = await relay.start()
    expires_at = int(time.time()) + ttl
    relay.token = relay_capability(port, expires_at, secrets.token_urlsafe(24))
    login_relay_registry.register(port, relay.token, expires_at)
    run = asyncio.create_task(relay.run())
    return server, relay, port, run


async def _bridge():
    communicator = ApplicationCommunicator(
        login_relay_consumer.LoginRelayConsumer.as_asgi(),
        {
            "type": "websocket",
            "path": "/ws/login-relay/",
            "headers": [],
            "query_string": b"",
            "user": None,
        },
    )
    await communicator.send_input({"type": "websocket.connect"})
    assert (await communicator.receive_output(timeout=5))["type"] == "websocket.accept"
    return communicator


async def _bytes_until(communicator, needle: bytes) -> bytes:
    seen = b""
    while needle not in seen:
        frame = await communicator.receive_output(timeout=5)
        assert frame["type"] == "websocket.send", frame
        seen += frame.get("bytes") or b""
    return seen


def test_route_is_registered():
    assert any("login-relay" in str(p.pattern) for p in routing.websocket_urlpatterns)


@pytest.mark.asyncio
async def test_bridge_carries_the_relay_protocol_to_the_callback():
    server, relay, port, run = await _relay()
    try:
        assert relay.loopback_server is not None
        bridge = await _bridge()
        await bridge.send_input(
            {
                "type": "websocket.receive",
                "bytes": f"{PROTOCOL} {relay.token}\n".encode(),
            }
        )
        assert await _bytes_until(bridge, b"OK\n") == b"OK\n"
        request = b"GET /callback?code=x&state=y HTTP/1.1\r\nHost: localhost\r\n\r\n"
        await bridge.send_input({"type": "websocket.receive", "bytes": request})
        assert (await _bytes_until(bridge, b"signed-in")).endswith(b"signed-in")
    finally:
        relay.done.set()
        await run
        server.close()


@pytest.mark.asyncio
async def test_bridge_refuses_unregistered_wrong_and_missing_hellos(monkeypatch):
    server, relay, port, run = await _relay()
    try:
        forged = relay_capability(
            port, int(time.time()) + 60, secrets.token_urlsafe(24)
        )
        for hello in (
            f"{PROTOCOL} {forged}\n".encode(),
            b"GET / HTTP/1.1\r\n\r\n",
            b"x" * 300,
        ):
            bridge = await _bridge()
            await bridge.send_input({"type": "websocket.receive", "bytes": hello})
            closed = await bridge.receive_output(timeout=5)
            assert closed == {"type": "websocket.close", "code": 4403}
        monkeypatch.setattr(login_relay_consumer, "HELLO_TIMEOUT_SECONDS", 0.2)
        bridge = await _bridge()
        assert await bridge.receive_output(timeout=5) == {
            "type": "websocket.close",
            "code": 4403,
        }
    finally:
        relay.done.set()
        await run
        server.close()


@pytest.mark.asyncio
async def test_expired_registration_is_refused():
    server, relay, port, run = await _relay()
    try:
        login_relay_registry.register(port, relay.token, int(time.time()) - 1)
        bridge = await _bridge()
        await bridge.send_input(
            {
                "type": "websocket.receive",
                "bytes": f"{PROTOCOL} {relay.token}\n".encode(),
            }
        )
        assert await bridge.receive_output(timeout=5) == {
            "type": "websocket.close",
            "code": 4403,
        }
    finally:
        relay.done.set()
        await run
        server.close()


def test_registry_keeps_only_a_hash(tmp_path):
    login_relay_registry.register(
        50123, "OBR1_50123_9_secret-nonce", int(time.time()) + 60
    )
    stored = (login_relay_registry.REGISTRY_DIR / "50123.json").read_text()
    assert "secret-nonce" not in stored
    assert (
        oct((login_relay_registry.REGISTRY_DIR / "50123.json").stat().st_mode)[-3:]
        == "600"
    )
    assert login_relay_registry.is_live(50123, "OBR1_50123_9_secret-nonce")
    assert not login_relay_registry.is_live(50123, "OBR1_50123_9_other")
    login_relay_registry.unregister(50123)
    assert not login_relay_registry.is_live(50123, "OBR1_50123_9_secret-nonce")


def test_parse_hello():
    cap = "OBR1_50123_1791700000_" + "a" * 32
    assert login_relay_consumer.parse_hello(f"{PROTOCOL} {cap}\n".encode()) == (
        50123,
        cap,
    )
    assert login_relay_consumer.parse_hello(f"{PROTOCOL} {cap}".encode()) is None
    assert (
        login_relay_consumer.parse_hello(f"{PROTOCOL} OBR1_80_1_{'a' * 32}\n".encode())
        is None
    )
    assert login_relay_consumer.parse_hello("\xff".encode("latin-1")) is None
