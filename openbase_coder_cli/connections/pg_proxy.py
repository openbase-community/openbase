"""Loopback Postgres proxy that terminates authentication.

A client connects to ``127.0.0.1:<port>`` with any user and no password; the
proxy opens the real upstream connection, authenticates with the leased
credentials it holds in memory, then splices bytes in both directions. The
agent-facing side therefore never sees the password, and the upstream only
ever sees the leased role.
"""

from __future__ import annotations

import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .pg_protocol import (
    AUTHENTICATION_OK_MESSAGE,
    CANCEL_REQUEST_CODE,
    GSSENC_REQUEST_CODE,
    SSL_REQUEST_CODE,
    ProtocolError,
    UpstreamError,
    UpstreamTarget,
    connect_upstream,
    encode_message,
    open_authenticated,
    parse_startup_params,
    read_startup,
)

LISTEN_HOST = "127.0.0.1"
# Startup parameters the client may set; user/database/password are the
# proxy's to decide.
FORWARDED_STARTUP_PARAMS = (
    "application_name",
    "client_encoding",
    "DateStyle",
    "TimeZone",
    "search_path",
    "extra_float_digits",
    "options",
)
COPY_CHUNK = 65536


@dataclass
class ProxyStats:
    accepted: int = 0
    open: int = 0
    failed: int = 0


def _pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while True:
            chunk = source.recv(COPY_CHUNK)
            if not chunk:
                break
            sink.sendall(chunk)
    except OSError:
        pass
    finally:
        for sock in (source, sink):
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class PostgresProxy:
    def __init__(
        self,
        port: int,
        target: UpstreamTarget,
        *,
        log: Callable[[str], None] | None = None,
        still_allowed: Callable[[], bool] | None = None,
        recheck_seconds: float = 60.0,
    ):
        self.port = port
        self._target = target
        self._log = log or (lambda _message: None)
        self._still_allowed = still_allowed
        self._recheck_seconds = recheck_seconds
        self._listener: socket.socket | None = None
        self._clients: set[socket.socket] = set()
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self.stats = ProxyStats()

    @property
    def local_url(self) -> str:
        return f"postgresql://{LISTEN_HOST}:{self.port}/{self._target.database}"

    def start(self) -> None:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((LISTEN_HOST, self.port))
        listener.listen(64)
        self._listener = listener
        self.port = listener.getsockname()[1]
        threading.Thread(target=self._accept_loop, daemon=True).start()
        if self._still_allowed is not None:
            threading.Thread(target=self._recheck_loop, daemon=True).start()

    def close(self) -> None:
        self._closed.set()
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def wait(self) -> None:
        while not self._closed.wait(0.5):
            pass

    def _recheck_loop(self) -> None:
        assert self._still_allowed is not None
        while not self._closed.wait(self._recheck_seconds):
            if not self._still_allowed():
                self._log("access revoked by Openbase Cloud; closing the proxy")
                self.close()
                return

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._closed.is_set():
            try:
                client, address = self._listener.accept()
            except OSError:
                return
            with self._lock:
                self._clients.add(client)
                self.stats.accepted += 1
            threading.Thread(
                target=self._handle_client, args=(client, address), daemon=True
            ).start()

    def _handle_client(self, client: socket.socket, address) -> None:
        peer = f"{address[0]}:{address[1]}"
        upstream: socket.socket | None = None
        try:
            client.settimeout(30.0)
            code, payload = read_startup(client)
            while code in (SSL_REQUEST_CODE, GSSENC_REQUEST_CODE):
                # Loopback traffic stays plaintext; the client retries without TLS.
                client.sendall(b"N")
                code, payload = read_startup(client)
            if code == CANCEL_REQUEST_CODE:
                self._forward_cancel(payload)
                return
            params = parse_startup_params(payload)
            startup = {
                key: value
                for key, value in params.items()
                if key in FORWARDED_STARTUP_PARAMS
            }
            startup.update(self._target.startup_params)
            upstream = open_authenticated(
                UpstreamTarget(
                    host=self._target.host,
                    port=self._target.port,
                    database=self._target.database,
                    user=self._target.user,
                    password=self._target.password,
                    ssl_mode=self._target.ssl_mode,
                    startup_params=startup,
                )
            )
            client.sendall(AUTHENTICATION_OK_MESSAGE)
            client.settimeout(None)
            upstream.settimeout(None)
            with self._lock:
                self.stats.open += 1
            self._log(
                f"connection from {peer} opened as {self._target.user}"
                f" (application {params.get('application_name') or 'unknown'})"
            )
            downstream = threading.Thread(
                target=_pump, args=(upstream, client), daemon=True
            )
            downstream.start()
            _pump(client, upstream)
            downstream.join()
            self._log(f"connection from {peer} closed")
        except (UpstreamError, ProtocolError, OSError) as exc:
            with self._lock:
                self.stats.failed += 1
            self._log(f"connection from {peer} failed: {exc}")
            try:
                client.sendall(
                    encode_message(
                        b"E",
                        b"SFATAL\0C08006\0M"
                        + f"openbase proxy: {exc}".encode()
                        + b"\0\0",
                    )
                )
            except OSError:
                pass
        finally:
            with self._lock:
                self._clients.discard(client)
                if upstream is not None and self.stats.open:
                    self.stats.open -= 1
            for sock in (client, upstream):
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass

    def _forward_cancel(self, payload: bytes) -> None:
        """Relay a CancelRequest (backend pid + secret key) to the upstream."""
        if len(payload) != 8:
            return
        try:
            sock = connect_upstream(
                self._target.host, self._target.port, self._target.ssl_mode, timeout=5.0
            )
        except (OSError, ProtocolError):
            return
        try:
            sock.sendall(struct.pack("!II", 16, CANCEL_REQUEST_CODE) + payload)
        finally:
            sock.close()


def wait_until_listening(port: int, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((LISTEN_HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False
