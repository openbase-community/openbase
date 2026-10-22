"""SigV4 signing (AWS documented vector), the loopback S3 proxy against a
fake upstream, and the built-in object client."""

from __future__ import annotations

import http.client
import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from openbase_coder_cli.connections import s3_client
from openbase_coder_cli.connections.s3_proxy import (
    S3ProxyServer,
    S3Target,
    decode_aws_chunked,
)
from openbase_coder_cli.connections.s3_signing import S3Credentials, sign_headers

AWS_EXAMPLE_CREDENTIALS = S3Credentials(
    "AKIAIOSFODNN7EXAMPLE", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
)
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_sigv4_matches_the_aws_get_object_example():
    headers = sign_headers(
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/test.txt",
        "",
        {"Range": "bytes=0-9"},
        AWS_EXAMPLE_CREDENTIALS,
        region="us-east-1",
        payload_hash=EMPTY_SHA256,
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert headers["Authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIAIOSFODNN7EXAMPLE/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
        "Signature=f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    )
    assert headers["x-amz-date"] == "20130524T000000Z"
    assert "host" not in {key.lower() for key in headers}


def test_sigv4_matches_the_aws_list_objects_example():
    headers = sign_headers(
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/",
        "max-keys=2&prefix=J",
        {},
        AWS_EXAMPLE_CREDENTIALS,
        region="us-east-1",
        payload_hash=EMPTY_SHA256,
        now=datetime(2013, 5, 24, tzinfo=UTC),
    )
    assert headers["Authorization"].endswith(
        "Signature=34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7"
    )


def test_decode_aws_chunked_strips_framing_and_trailer():
    body = b"5;chunk-signature=abc\r\nhello\r\n1\r\n!\r\n0\r\nx-amz-checksum-crc32:AAAA\r\n\r\n"
    assert decode_aws_chunked(body) == b"hello!"


class FakeS3(BaseHTTPRequestHandler):
    requests: list[dict] = []
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):  # noqa: A002
        return

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        FakeS3.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": dict(self.headers.items()),
                "body": body,
            }
        )
        if self.path.startswith("/bucket/missing"):
            payload = b"<Error><Code>NoSuchKey</Code><Message>gone</Message></Error>"
            self.send_response(404)
        elif self.command == "GET" and "list-type=2" in self.path:
            payload = (
                b'<?xml version="1.0"?><ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                b"<IsTruncated>false</IsTruncated><Contents><Key>a.txt</Key><Size>3</Size>"
                b"<LastModified>2026-10-22T00:00:00Z</LastModified></Contents></ListBucketResult>"
            )
            self.send_response(200)
        else:
            payload = b"object-bytes" if self.command == "GET" else b""
            self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("ETag", '"etag"')
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)

    do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = _handle


@pytest.fixture
def fake_s3():
    FakeS3.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeS3)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _target(server) -> S3Target:
    return S3Target(
        endpoint=f"http://127.0.0.1:{server.server_address[1]}",
        bucket="bucket",
        region="auto",
    )


def _proxy(server, **kwargs) -> S3ProxyServer:
    proxy = S3ProxyServer(
        0,
        _target(server),
        S3Credentials("AKID", "secret"),
        read_only=kwargs.pop("read_only", False),
        **kwargs,
    )
    proxy.start()
    return proxy


def _request(proxy, method, path, body=b"", headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", proxy.port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response, payload


def test_proxy_resigns_requests_with_the_leased_key(fake_s3):
    proxy = _proxy(fake_s3)
    try:
        response, payload = _request(
            proxy,
            "PUT",
            "/bucket/dir/file.txt?x-id=PutObject",
            body=b"hello",
            headers={
                "Authorization": "AWS4-HMAC-SHA256 Credential=openbase/placeholder",
                "x-amz-date": "20000101T000000Z",
                "x-amz-content-sha256": "deadbeef",
                "Content-Type": "text/plain",
            },
        )
        assert response.status == 200
        upstream = FakeS3.requests[-1]
        assert upstream["method"] == "PUT"
        assert upstream["path"] == "/bucket/dir/file.txt?x-id=PutObject"
        assert upstream["body"] == b"hello"
        assert upstream["headers"]["Authorization"].startswith(
            "AWS4-HMAC-SHA256 Credential=AKID/"
        )
        assert upstream["headers"]["x-amz-content-sha256"] == "UNSIGNED-PAYLOAD"
        assert upstream["headers"]["x-amz-date"] != "20000101T000000Z"
        assert upstream["headers"]["Content-Type"] == "text/plain"
        response, payload = _request(proxy, "GET", "/bucket/dir/file.txt")
        assert (response.status, payload) == (200, b"object-bytes")
    finally:
        proxy.close()


def test_proxy_decodes_aws_chunked_uploads(fake_s3):
    proxy = _proxy(fake_s3)
    try:
        body = b"3;chunk-signature=x\r\nabc\r\n0\r\nx-amz-checksum-crc32:AAAA\r\n\r\n"
        response, _ = _request(
            proxy,
            "PUT",
            "/bucket/k",
            body=body,
            headers={
                "Content-Encoding": "aws-chunked",
                "x-amz-decoded-content-length": "3",
                "x-amz-trailer": "x-amz-checksum-crc32",
            },
        )
        assert response.status == 200
        upstream = FakeS3.requests[-1]
        assert upstream["body"] == b"abc"
        assert "Content-Encoding" not in upstream["headers"]
        assert "x-amz-trailer" not in upstream["headers"]
    finally:
        proxy.close()


def test_proxy_refuses_other_buckets_and_writes_when_read_only(fake_s3):
    proxy = _proxy(fake_s3, read_only=True)
    try:
        response, payload = _request(proxy, "GET", "/other/key")
        assert response.status == 403 and b"AccessDenied" in payload
        response, payload = _request(proxy, "PUT", "/bucket/key", body=b"x")
        assert response.status == 403 and b"read-only" in payload
        response, _ = _request(proxy, "HEAD", "/bucket/key")
        assert response.status == 200
        assert [item["method"] for item in FakeS3.requests] == ["HEAD"]
    finally:
        proxy.close()


def test_proxy_closes_when_access_is_revoked(fake_s3):
    allowed = {"value": True}
    proxy = _proxy(
        fake_s3, still_allowed=lambda: allowed["value"], recheck_seconds=0.05
    )
    try:
        allowed["value"] = False
        proxy._closed.wait(2.0)
        assert proxy._closed.is_set()
    finally:
        proxy.close()


def test_object_client_lists_gets_puts_and_deletes(fake_s3, tmp_path):
    target = _target(fake_s3)
    credentials = S3Credentials("AKID", "secret")
    objects = list(s3_client.list_objects(target, credentials, "a"))
    assert [(item.key, item.size) for item in objects] == [("a.txt", 3)]
    assert "prefix=a" in FakeS3.requests[-1]["path"]
    source = tmp_path / "up.bin"
    source.write_bytes(b"payload")
    s3_client.put_object(
        target,
        credentials,
        "dir/up bin",
        source,
        content_type="application/octet-stream",
    )
    assert FakeS3.requests[-1]["path"] == "/bucket/dir/up%20bin"
    assert FakeS3.requests[-1]["body"] == b"payload"
    destination = tmp_path / "down.bin"
    assert s3_client.get_object(target, credentials, "dir/up bin", destination) == len(
        b"object-bytes"
    )
    assert destination.read_bytes() == b"object-bytes"
    s3_client.delete_object(target, credentials, "dir/up bin")
    assert FakeS3.requests[-1]["method"] == "DELETE"
    with pytest.raises(s3_client.S3RequestError, match="NoSuchKey"):
        s3_client.get_object(target, credentials, "missing", Path(tmp_path / "x"))
