"""Loopback S3 proxy: accepts unsigned path-style S3 requests for one bucket
on ``http://127.0.0.1:<port>`` and forwards them to the real endpoint signed
with the leased key, so the agent-facing tools never hold the credentials.

Any S3 client works when pointed at the local endpoint with path-style
addressing and placeholder credentials (the proxy ignores the client's own
signature). Requests outside the leased bucket are refused locally, and a
read-only proxy refuses everything but GET and HEAD."""

from __future__ import annotations

import http.client
import socket
import ssl
import threading
from collections.abc import Callable
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .s3_signing import S3Credentials, sign_headers

LISTEN_HOST = "127.0.0.1"
READ_METHODS = ("GET", "HEAD")
# Client-side signing and hop-by-hop headers are never forwarded.
DROPPED_REQUEST_HEADERS = {
    "authorization",
    "host",
    "connection",
    "keep-alive",
    "proxy-connection",
    "expect",
    "transfer-encoding",
    "content-length",
    "x-amz-date",
    "x-amz-content-sha256",
    "x-amz-security-token",
    "x-amz-decoded-content-length",
    "x-amz-trailer",
    "content-encoding",
}
DROPPED_RESPONSE_HEADERS = {"connection", "keep-alive", "transfer-encoding"}
COPY_CHUNK = 65536


@dataclass(frozen=True)
class S3Target:
    endpoint: str
    bucket: str
    region: str = "auto"
    path_style: bool = True

    @property
    def upstream_host(self) -> str:
        parts = urlsplit(self.endpoint)
        host = parts.netloc
        return host if self.path_style else f"{self.bucket}.{host}"

    @property
    def upstream_scheme(self) -> str:
        return urlsplit(self.endpoint).scheme or "https"

    @property
    def base_path(self) -> str:
        return urlsplit(self.endpoint).path.rstrip("/")

    def upstream_path(self, key_path: str) -> str:
        """Path on the upstream for ``key_path`` (``/key`` inside the bucket)."""
        if self.path_style:
            return f"{self.base_path}/{self.bucket}{key_path}"
        return f"{self.base_path}{key_path}" or "/"


def decode_aws_chunked(body: bytes) -> bytes:
    """Undo ``Content-Encoding: aws-chunked`` framing (SDK streaming uploads)."""
    output = bytearray()
    offset = 0
    while offset < len(body):
        line_end = body.index(b"\r\n", offset)
        size_text = body[offset:line_end].split(b";", 1)[0]
        size = int(size_text, 16)
        offset = line_end + 2
        if size == 0:
            break
        output.extend(body[offset : offset + size])
        offset += size + 2
    return bytes(output)


class S3ProxyServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        port: int,
        target: S3Target,
        credentials: S3Credentials,
        *,
        read_only: bool,
        log: Callable[[str], None] | None = None,
        still_allowed: Callable[[], bool] | None = None,
        recheck_seconds: float = 60.0,
    ):
        super().__init__((LISTEN_HOST, port), S3ProxyHandler)
        self.target = target
        self.credentials = credentials
        self.read_only = read_only
        self.log = log or (lambda _message: None)
        self._still_allowed = still_allowed
        self._recheck_seconds = recheck_seconds
        self._closed = threading.Event()
        self.port = self.server_address[1]

    @property
    def local_url(self) -> str:
        return f"http://{LISTEN_HOST}:{self.port}"

    def start(self) -> None:
        threading.Thread(target=self.serve_forever, daemon=True).start()
        if self._still_allowed is not None:
            threading.Thread(target=self._recheck_loop, daemon=True).start()

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self.shutdown()
        self.server_close()

    def wait(self) -> None:
        while not self._closed.wait(0.5):
            pass

    def _recheck_loop(self) -> None:
        assert self._still_allowed is not None
        while not self._closed.wait(self._recheck_seconds):
            if not self._still_allowed():
                self.log("access revoked by Openbase Cloud; closing the proxy")
                threading.Thread(target=self.close, daemon=True).start()
                return


class S3ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server: S3ProxyServer

    def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler API
        return

    def _refuse(self, status: int, code: str, message: str) -> None:
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?><Error><Code>{code}</Code>'
            f"<Message>{message}</Message></Error>"
        ).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _bucket_key_path(self) -> str | None:
        """``/key`` for a path-style request inside the leased bucket, else None."""
        path = urlsplit(self.path).path
        bucket_prefix = f"/{self.server.target.bucket}"
        if path == bucket_prefix:
            return "/"
        if path.startswith(bucket_prefix + "/"):
            return path[len(bucket_prefix) :]
        return None

    def _proxy(self) -> None:
        target = self.server.target
        key_path = self._bucket_key_path()
        if key_path is None:
            self._refuse(
                403,
                "AccessDenied",
                f"Only bucket {target.bucket} is reachable through this proxy.",
            )
            return
        if self.server.read_only and self.command not in READ_METHODS:
            self._refuse(403, "AccessDenied", "This proxy is read-only.")
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        if "aws-chunked" in (self.headers.get("Content-Encoding") or ""):
            body = decode_aws_chunked(body)
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in DROPPED_REQUEST_HEADERS
        }
        if body or self.command in ("PUT", "POST"):
            headers["Content-Length"] = str(len(body))
        query = urlsplit(self.path).query
        upstream_path = target.upstream_path(key_path)
        signed = sign_headers(
            self.command,
            target.upstream_host,
            upstream_path,
            query,
            headers,
            self.server.credentials,
            region=target.region,
        )
        connection_class = (
            http.client.HTTPSConnection
            if target.upstream_scheme == "https"
            else http.client.HTTPConnection
        )
        kwargs = {"timeout": 300}
        if target.upstream_scheme == "https":
            kwargs["context"] = ssl.create_default_context()
        connection = connection_class(target.upstream_host, **kwargs)
        try:
            connection.request(
                self.command,
                upstream_path + (f"?{query}" if query else ""),
                body=body,
                headers=signed,
            )
            response = connection.getresponse()
            self.send_response(response.status, response.reason)
            chunked = False
            for key, value in response.getheaders():
                if key.lower() in DROPPED_RESPONSE_HEADERS:
                    chunked = chunked or (
                        key.lower() == "transfer-encoding" and "chunked" in value
                    )
                    continue
                self.send_header(key, value)
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            if self.command == "HEAD":
                return
            while True:
                chunk = response.read(COPY_CHUNK)
                if not chunk:
                    break
                if chunked:
                    self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                else:
                    self.wfile.write(chunk)
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
            self.server.log(
                f"{self.command} {target.bucket}{key_path} -> {response.status}"
            )
        except (OSError, http.client.HTTPException) as exc:
            self.server.log(f"{self.command} {target.bucket}{key_path} failed: {exc}")
            self._refuse(502, "ProxyError", f"Upstream request failed: {exc}")
        finally:
            connection.close()

    do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _proxy


def wait_until_listening(port: int, timeout: float = 5.0) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((LISTEN_HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False
