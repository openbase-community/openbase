"""WebSocket bridge from the phone to a login callback relay on this host.

On a desktop or VM backend the phone normally opens a TCP connection to the
relay on this host's VPN address. A host firewall can drop that connection
(macOS blocks an unsigned Python silently with stealth mode on), while the
local API stays reachable through the VPN serve route that the signed VPN
component terminates. So the phone may instead open ``ws/login-relay/`` on
that route and speak the same relay protocol inside binary frames.

The bridge authenticates nothing on behalf of a user: the first frame must be
the relay hello (``OPENBASE-LOOPBACK/1 <capability>``) of a relay that is live
right now (see ``login_relay_registry``). Only then does it dial that relay on
loopback and pass the hello through, so the relay still checks the capability
itself and the bridge can never be used to reach any other local port.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from channels.generic.websocket import AsyncWebsocketConsumer

from openbase_coder_cli import login_relay_registry
from openbase_coder_cli.callback_relay import PROTOCOL

logger = logging.getLogger(__name__)

HELLO_RE = re.compile(
    rf"^{re.escape(PROTOCOL)} (OBR1_(\d{{4,5}})_\d+_[A-Za-z0-9_-]{{32,}})\n$"
)
HELLO_TIMEOUT_SECONDS = 3
MAX_HELLO_BYTES = 256
CHUNK_BYTES = 16384


def parse_hello(data: bytes) -> tuple[int, str] | None:
    """The relay port and capability a valid hello names, else None."""
    if len(data) > MAX_HELLO_BYTES:
        return None
    try:
        match = HELLO_RE.fullmatch(data.decode("ascii"))
    except UnicodeDecodeError:
        return None
    if match is None:
        return None
    capability, port = match.group(1), int(match.group(2))
    if not 1024 <= port <= 65535 or len(capability) > 128:
        return None
    return port, capability


class LoginRelayConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        self.upstream: asyncio.StreamWriter | None = None
        self.pump: asyncio.Task | None = None
        self.hello_deadline: asyncio.TimerHandle | None = None
        await self.accept()
        self.hello_deadline = asyncio.get_running_loop().call_later(
            HELLO_TIMEOUT_SECONDS, lambda: asyncio.ensure_future(self._reject())
        )

    async def _reject(self):
        if self.upstream is None:
            await self.close(code=4403)

    async def receive(self, text_data=None, bytes_data=None):
        data = bytes_data if bytes_data is not None else (text_data or "").encode()
        if self.upstream is not None:
            self.upstream.write(data)
            await self.upstream.drain()
            return
        if self.hello_deadline is not None:
            self.hello_deadline.cancel()
        hello = parse_hello(data)
        if hello is None or not login_relay_registry.is_live(*hello):
            logger.info("login relay bridge: rejected a hello")
            await self.close(code=4403)
            return
        port, _capability = hello
        try:
            reader, self.upstream = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", port), 3
            )
        except (OSError, TimeoutError):
            await self.close(code=1011)
            return
        self.upstream.write(data)
        await self.upstream.drain()
        self.pump = asyncio.create_task(self._pump(reader))

    async def _pump(self, reader: asyncio.StreamReader):
        try:
            while data := await reader.read(CHUNK_BYTES):
                await self.send(bytes_data=data)
        except OSError:
            pass
        finally:
            with contextlib.suppress(Exception):
                await self.close()

    async def disconnect(self, code):
        if self.hello_deadline is not None:
            self.hello_deadline.cancel()
        if self.upstream is not None:
            self.upstream.close()
        if self.pump is not None and self.pump is not asyncio.current_task():
            self.pump.cancel()
