"""Short-lived, capability-authenticated TCP relay for a CLI's loopback listener.

The VPN exposes only this relay, never the CLI port. The phone sends
``OPENBASE-LOOPBACK/1 <token>\n`` and waits for ``OK\n`` before copying bytes.
The token travels over WireGuard and is removed before the CLI sees traffic.
No credentials are passed in argv, written to disk, or logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
import select
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass

import psutil

PROTOCOL = "OPENBASE-LOOPBACK/1"


@dataclass(frozen=True)
class ListenerOwner:
    pid: int
    created: float
    fd: int
    host: str
    port: int
    socket_identity: str | None = None

    @classmethod
    def find(cls, port: int) -> ListenerOwner:
        """Fail closed for ambiguous, wildcard, foreign-user or missing listeners."""
        matches = [
            c
            for c in psutil.net_connections(kind="tcp")
            if c.status == psutil.CONN_LISTEN and c.laddr.port == port
        ]
        if not matches or any(c.laddr.ip not in {"127.0.0.1", "::1"} for c in matches):
            raise ValueError("callback needs a loopback-only listener")
        if len({c.pid for c in matches}) != 1 or matches[0].pid is None:
            raise ValueError("callback listener owner is ambiguous")
        conn = next((c for c in matches if c.laddr.ip == "127.0.0.1"), matches[0])
        process = psutil.Process(conn.pid)
        if process.uids().real != os.getuid():
            raise ValueError("callback listener belongs to another user")
        identity = cls.identity(process.pid, conn.fd)
        return cls(
            process.pid, process.create_time(), conn.fd, conn.laddr.ip, port, identity
        )

    @staticmethod
    def identity(pid: int, fd: int) -> str | None:
        if sys.platform == "linux":
            return os.readlink(f"/proc/{pid}/fd/{fd}")
        return None

    def process_alive(self) -> bool:
        try:
            return psutil.Process(self.pid).create_time() == self.created
        except psutil.Error:
            return False

    def owns_connection(self, peer: tuple) -> bool:
        try:
            process = psutil.Process(self.pid)
            return process.create_time() == self.created and any(
                connection.laddr.ip == self.host
                and connection.laddr.port == self.port
                and connection.raddr
                and connection.raddr.ip == peer[0]
                and connection.raddr.port == peer[1]
                for connection in process.net_connections(kind="tcp")
            )
        except (psutil.Error, OSError):
            return False

    def alive(self) -> bool:
        try:
            if (
                self.socket_identity is not None
                and self.identity(self.pid, self.fd) != self.socket_identity
            ):
                return False
            process = psutil.Process(self.pid)
            return process.create_time() == self.created and any(
                c.fd == self.fd
                and c.status == psutil.CONN_LISTEN
                and c.laddr.ip == self.host
                and c.laddr.port == self.port
                for c in process.net_connections(kind="tcp")
            )
        except (psutil.Error, OSError):
            return False


class CallbackRelay:
    def __init__(self, owner: ListenerOwner, token: str, ttl: float):
        self.owner, self.token = owner, token
        self.deadline = time.monotonic() + ttl
        self.done = asyncio.Event()
        self.active: set[asyncio.Task] = set()
        self.connected: set[asyncio.Task] = set()
        self.server: asyncio.Server | None = None

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0, limit=256)
        return self.server.sockets[0].getsockname()[1]

    async def run(self) -> None:
        deadline = self.deadline
        try:
            while not self.done.is_set() and time.monotonic() < deadline:
                if not await asyncio.to_thread(self.owner.alive):
                    self.server.close()
                    for task in self.active - self.connected:
                        task.cancel()
                    if not self.connected or not await asyncio.to_thread(
                        self.owner.process_alive
                    ):
                        break
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self.done.wait(),
                        min(0.5, max(0.01, deadline - time.monotonic())),
                    )
        finally:
            self.server.close()
            tasks = list(self.active)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.server.wait_closed()

    async def handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        task = asyncio.current_task()
        if (
            len(self.active) >= 8
            or self.done.is_set()
            or time.monotonic() >= self.deadline
        ):
            writer.close()
            return
        self.active.add(task)
        timeout = asyncio.get_running_loop().call_later(
            max(0, self.deadline - time.monotonic()), task.cancel
        )
        upstream = None
        try:
            hello = await asyncio.wait_for(reader.readline(), 3)
            expected = f"{PROTOCOL} {self.token}\n".encode()
            if not hmac.compare_digest(hello, expected) or not await asyncio.to_thread(
                self.owner.alive
            ):
                return
            writer.write(b"OK\n")
            await writer.drain()
            # A health handshake or browser preconnect must not touch the CLI
            # listener (some CLIs accept only one connection).
            first = await reader.read(16384)
            if not first:
                return
            if not await asyncio.to_thread(self.owner.alive):
                return
            self.connected.add(task)
            source, upstream = await asyncio.wait_for(
                asyncio.open_connection(self.owner.host, self.owner.port), 3
            )
            if not await asyncio.to_thread(
                self.owner.alive
            ) and not await asyncio.to_thread(
                self.owner.owns_connection, upstream.get_extra_info("sockname")
            ):
                return
            upstream.write(first)
            await upstream.drain()

            async def pump(src, dst):
                count = 0
                while data := await src.read(16384):
                    dst.write(data)
                    await dst.drain()
                    count += len(data)
                if dst.can_write_eof():
                    dst.write_eof()
                return count

            send = asyncio.create_task(pump(reader, upstream))
            try:
                await pump(source, writer)
                # Callback servers normally close the response while browsers
                # keep their write half open. Do not wait on the browser FIN.
                # Keep multi-page local login starts and redirects alive. The
                # owner closing its listener (or TTL) is the completion signal.
            finally:
                send.cancel()
                await asyncio.gather(send, return_exceptions=True)
        except (OSError, ValueError, TimeoutError):
            # Invalid capability, preconnect, disconnect: leave the real login
            # available until an authenticated exchange or the deadline.
            pass
        finally:
            timeout.cancel()
            writer.close()
            if upstream:
                upstream.close()
            self.active.discard(task)
            self.connected.discard(task)


def start_relay(port: int, token: str, ttl: int = 600, *, expires_at: int) -> int:
    """Start an isolated child; acknowledge only once its VPN listener is up."""
    owner = ListenerOwner.find(port)
    child = subprocess.Popen(
        [sys.executable, "-m", "openbase_coder_cli.callback_relay"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        child.stdin.write(
            json.dumps(
                {
                    "owner": asdict(owner),
                    "token": token,
                    "ttl": ttl,
                    "expires_at": expires_at,
                }
            ).encode()
            + b"\n"
        )
        child.stdin.close()
        if not select.select([child.stdout], [], [], 4)[0]:
            raise TimeoutError("callback relay startup timed out")
        payload = json.loads(child.stdout.readline())
        if "port" not in payload:
            raise RuntimeError("callback relay could not expose its listener")
        return int(payload["port"])
    except BaseException:
        child.terminate()
        child.wait(timeout=2)
        raise
    finally:
        child.stdout.close()


async def _main(config: dict) -> None:
    from openbase_coder_cli.services.tunneld import (
        tunneld_add_forward,
        tunneld_remove_forward,
    )

    relay = CallbackRelay(
        ListenerOwner(**config["owner"]),
        config["token"],
        min(600, config["ttl"], config["expires_at"] - time.time()),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, relay.done.set)
    port = await relay.start()
    from openbase_coder_cli.login_callback import relay_capability

    relay.token = relay_capability(port, config["expires_at"], config["token"])
    exposed = False
    try:
        # The authentication handshake itself carries bytes both ways. Only
        # this relay can decide when the actual callback has completed.
        await asyncio.to_thread(
            tunneld_add_forward,
            port,
            ttl_seconds=min(600, config["ttl"]),
            one_shot=False,
        )
        exposed = True
        print(json.dumps({"port": port}), flush=True)
        sys.stdout.close()
        await relay.run()
    finally:
        if exposed:
            await asyncio.to_thread(tunneld_remove_forward, port)


if __name__ == "__main__":
    asyncio.run(_main(json.loads(sys.stdin.readline())))
