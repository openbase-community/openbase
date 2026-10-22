"""Just enough of the PostgreSQL v3 wire protocol for the local proxy and the
built-in query client: framing, startup, TLS negotiation and client-side
authentication (cleartext, MD5 and SCRAM-SHA-256).

No driver dependency: the CLI must run on a bare cloud workspace, and the
proxy has to perform the upstream handshake itself so the agent-facing side
never needs the password.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import socket
import ssl
import struct
from dataclasses import dataclass, field

PROTOCOL_VERSION = 196608  # 3.0
SSL_REQUEST_CODE = 80877103
GSSENC_REQUEST_CODE = 80877104
CANCEL_REQUEST_CODE = 80877102

AUTH_OK = 0
AUTH_CLEARTEXT = 3
AUTH_MD5 = 5
AUTH_SASL = 10
AUTH_SASL_CONTINUE = 11
AUTH_SASL_FINAL = 12

SCRAM_SHA_256 = "SCRAM-SHA-256"
SSL_MODES = ("disable", "allow", "prefer", "require", "verify-ca", "verify-full")

AUTHENTICATION_OK_MESSAGE = b"R" + struct.pack("!II", 8, AUTH_OK)


class ProtocolError(RuntimeError):
    """The peer sent something this implementation cannot handle."""


class UpstreamError(RuntimeError):
    """The server answered with an ErrorResponse."""

    def __init__(self, fields: dict[str, str]):
        self.fields = fields
        code = fields.get("C", "")
        message = fields.get("M", "server error")
        super().__init__(f"{message} ({code})" if code else message)


@dataclass(frozen=True)
class UpstreamTarget:
    host: str
    port: int
    database: str
    user: str
    password: str
    ssl_mode: str = "require"
    startup_params: dict[str, str] = field(default_factory=dict)


def read_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise ConnectionError("connection closed")
        chunks.extend(chunk)
    return bytes(chunks)


def read_message(sock: socket.socket) -> tuple[bytes, bytes]:
    """Read one typed message: ``(type, payload)`` without the length word."""
    kind = read_exact(sock, 1)
    (length,) = struct.unpack("!I", read_exact(sock, 4))
    if length < 4:
        raise ProtocolError("invalid message length")
    return kind, read_exact(sock, length - 4)


def encode_message(kind: bytes, payload: bytes) -> bytes:
    return kind + struct.pack("!I", len(payload) + 4) + payload


def read_startup(sock: socket.socket) -> tuple[int, bytes]:
    """Read an untyped startup packet: ``(code, payload after the code)``."""
    (length,) = struct.unpack("!I", read_exact(sock, 4))
    if length < 8 or length > 10_000:
        raise ProtocolError("invalid startup packet length")
    body = read_exact(sock, length - 4)
    (code,) = struct.unpack("!I", body[:4])
    return code, body[4:]


def parse_startup_params(payload: bytes) -> dict[str, str]:
    parts = payload.split(b"\0")
    params: dict[str, str] = {}
    for index in range(0, len(parts) - 1, 2):
        key = parts[index].decode("utf-8", "replace")
        if not key:
            break
        params[key] = parts[index + 1].decode("utf-8", "replace")
    return params


def encode_startup(params: dict[str, str]) -> bytes:
    body = struct.pack("!I", PROTOCOL_VERSION)
    for key, value in params.items():
        body += key.encode() + b"\0" + value.encode() + b"\0"
    body += b"\0"
    return struct.pack("!I", len(body) + 4) + body


def parse_error_fields(payload: bytes) -> dict[str, str]:
    fields: dict[str, str] = {}
    for item in payload.split(b"\0"):
        if item:
            fields[chr(item[0])] = item[1:].decode("utf-8", "replace")
    return fields


def _ssl_context(ssl_mode: str) -> ssl.SSLContext:
    if ssl_mode == "verify-full":
        return ssl.create_default_context()
    context = ssl.create_default_context()
    context.check_hostname = False
    if ssl_mode == "verify-ca":
        context.verify_mode = ssl.CERT_REQUIRED
    else:
        # libpq semantics: "require" encrypts without verifying the peer.
        context.verify_mode = ssl.CERT_NONE
    return context


def connect_upstream(
    host: str, port: int, ssl_mode: str, timeout: float = 15.0
) -> socket.socket:
    """Open a TCP connection and negotiate TLS the way libpq's sslmode does."""
    if ssl_mode not in SSL_MODES:
        raise ValueError(f"unknown sslmode {ssl_mode!r}")
    sock = socket.create_connection((host, port), timeout=timeout)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    if ssl_mode == "disable":
        return sock
    sock.sendall(struct.pack("!II", 8, SSL_REQUEST_CODE))
    answer = read_exact(sock, 1)
    if answer == b"S":
        return _ssl_context(ssl_mode).wrap_socket(sock, server_hostname=host)
    if answer == b"N" and ssl_mode in ("allow", "prefer"):
        return sock
    sock.close()
    raise ProtocolError(f"{host} does not accept TLS connections (sslmode={ssl_mode})")


def _md5_password(user: str, password: str, salt: bytes) -> bytes:
    inner = hashlib.md5((password + user).encode()).hexdigest()
    return b"md5" + hashlib.md5(inner.encode() + salt).hexdigest().encode()


class ScramClient:
    """SCRAM-SHA-256 client (RFC 7677) without channel binding."""

    def __init__(self, password: str, nonce: str | None = None, username: str = ""):
        # Postgres takes the user from the startup packet and ignores ``n=``;
        # the username only matters for conformance vectors.
        self._password = password.encode("utf-8")
        self._nonce = nonce or base64.b64encode(secrets.token_bytes(18)).decode()
        self._client_first_bare = f"n={username},r={self._nonce}"
        self._server_signature = b""

    def client_first(self) -> bytes:
        return ("n,," + self._client_first_bare).encode()

    def client_final(self, server_first: bytes) -> bytes:
        attributes = dict(
            item.split("=", 1) for item in server_first.decode().split(",")
        )
        server_nonce = attributes["r"]
        if not server_nonce.startswith(self._nonce):
            raise ProtocolError("SCRAM server nonce does not extend the client nonce")
        salt = base64.b64decode(attributes["s"])
        iterations = int(attributes["i"])
        salted = hashlib.pbkdf2_hmac("sha256", self._password, salt, iterations)
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        stored_key = hashlib.sha256(client_key).digest()
        without_proof = f"c={base64.b64encode(b'n,,').decode()},r={server_nonce}"
        auth_message = ",".join(
            [self._client_first_bare, server_first.decode(), without_proof]
        ).encode()
        client_signature = hmac.new(stored_key, auth_message, hashlib.sha256).digest()
        proof = bytes(a ^ b for a, b in zip(client_key, client_signature, strict=False))
        server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        self._server_signature = hmac.new(
            server_key, auth_message, hashlib.sha256
        ).digest()
        return f"{without_proof},p={base64.b64encode(proof).decode()}".encode()

    def verify_server_final(self, server_final: bytes) -> None:
        attributes = dict(
            item.split("=", 1) for item in server_final.decode().split(",")
        )
        if "e" in attributes:
            raise ProtocolError(f"SCRAM failure: {attributes['e']}")
        expected = base64.b64encode(self._server_signature).decode()
        if not hmac.compare_digest(attributes.get("v", ""), expected):
            raise ProtocolError("SCRAM server signature mismatch")


def authenticate(sock: socket.socket, target: UpstreamTarget) -> None:
    """Send the startup packet and complete authentication.

    Returns once the server sent AuthenticationOk; the parameter status,
    backend key and ReadyForQuery messages that follow are left unread so a
    proxy can relay them verbatim.
    """
    params = {"user": target.user, "database": target.database}
    params.update(target.startup_params)
    sock.sendall(encode_startup(params))
    scram: ScramClient | None = None
    while True:
        kind, payload = read_message(sock)
        if kind == b"E":
            raise UpstreamError(parse_error_fields(payload))
        if kind != b"R":
            raise ProtocolError(f"unexpected message {kind!r} during authentication")
        (code,) = struct.unpack("!I", payload[:4])
        if code == AUTH_OK:
            return
        if code == AUTH_CLEARTEXT:
            sock.sendall(encode_message(b"p", target.password.encode() + b"\0"))
        elif code == AUTH_MD5:
            sock.sendall(
                encode_message(
                    b"p",
                    _md5_password(target.user, target.password, payload[4:8]) + b"\0",
                )
            )
        elif code == AUTH_SASL:
            mechanisms = [m.decode() for m in payload[4:].split(b"\0") if m]
            if SCRAM_SHA_256 not in mechanisms:
                raise ProtocolError(
                    f"server offers no supported SASL mechanism ({', '.join(mechanisms)})"
                )
            scram = ScramClient(target.password)
            first = scram.client_first()
            sock.sendall(
                encode_message(
                    b"p",
                    SCRAM_SHA_256.encode()
                    + b"\0"
                    + struct.pack("!I", len(first))
                    + first,
                )
            )
        elif code == AUTH_SASL_CONTINUE:
            if scram is None:
                raise ProtocolError("SASL continue before SASL start")
            sock.sendall(encode_message(b"p", scram.client_final(payload[4:])))
        elif code == AUTH_SASL_FINAL:
            if scram is None:
                raise ProtocolError("SASL final before SASL start")
            scram.verify_server_final(payload[4:])
        else:
            raise ProtocolError(f"unsupported authentication method {code}")


def open_authenticated(target: UpstreamTarget, timeout: float = 15.0) -> socket.socket:
    sock = connect_upstream(target.host, target.port, target.ssl_mode, timeout=timeout)
    try:
        authenticate(sock, target)
    except Exception:
        sock.close()
        raise
    return sock
