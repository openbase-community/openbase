"""Built-in object operations for ``openbase-coder storage ls/get/put/rm``:
direct SigV4 requests with the leased key, no SDK needed on the host."""

from __future__ import annotations

import http.client
import ssl
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode

from .s3_proxy import S3Target
from .s3_signing import S3Credentials, sign_headers

S3_NAMESPACE = "{http://s3.amazonaws.com/doc/2006-03-01/}"
COPY_CHUNK = 65536


class S3RequestError(RuntimeError):
    def __init__(self, status: int, body: bytes):
        self.status = status
        code = message = ""
        try:
            root = ET.fromstring(body)
            code = root.findtext("Code") or ""
            message = root.findtext("Message") or ""
        except ET.ParseError:
            message = body[:200].decode("utf-8", "replace")
        super().__init__(f"HTTP {status} {code}: {message}".strip())


@dataclass(frozen=True)
class S3Object:
    key: str
    size: int
    last_modified: str


def _request(
    target: S3Target,
    credentials: S3Credentials,
    method: str,
    key_path: str,
    *,
    query: dict[str, str] | None = None,
    body: bytes = b"",
    headers: dict[str, str] | None = None,
) -> http.client.HTTPResponse:
    query_string = urlencode(query or {}, quote_via=quote)
    path = target.upstream_path(key_path)
    request_headers = dict(headers or {})
    if body or method == "PUT":
        request_headers["Content-Length"] = str(len(body))
    signed = sign_headers(
        method,
        target.upstream_host,
        path,
        query_string,
        request_headers,
        credentials,
        region=target.region,
    )
    if target.upstream_scheme == "https":
        connection = http.client.HTTPSConnection(
            target.upstream_host, timeout=300, context=ssl.create_default_context()
        )
    else:
        connection = http.client.HTTPConnection(target.upstream_host, timeout=300)
    connection.request(
        method,
        path + (f"?{query_string}" if query_string else ""),
        body=body,
        headers=signed,
    )
    response = connection.getresponse()
    if response.status >= 400:
        payload = response.read()
        connection.close()
        raise S3RequestError(response.status, payload)
    return response


def list_objects(
    target: S3Target, credentials: S3Credentials, prefix: str = "", *, limit: int = 1000
) -> Iterator[S3Object]:
    token = ""
    remaining = limit
    while remaining > 0:
        query = {"list-type": "2", "max-keys": str(min(remaining, 1000))}
        if prefix:
            query["prefix"] = prefix
        if token:
            query["continuation-token"] = token
        response = _request(target, credentials, "GET", "/", query=query)
        root = ET.fromstring(response.read())
        response.close()
        for item in root.findall(f"{S3_NAMESPACE}Contents"):
            yield S3Object(
                key=item.findtext(f"{S3_NAMESPACE}Key") or "",
                size=int(item.findtext(f"{S3_NAMESPACE}Size") or 0),
                last_modified=item.findtext(f"{S3_NAMESPACE}LastModified") or "",
            )
            remaining -= 1
            if remaining <= 0:
                return
        if root.findtext(f"{S3_NAMESPACE}IsTruncated") != "true":
            return
        token = root.findtext(f"{S3_NAMESPACE}NextContinuationToken") or ""
        if not token:
            return


def key_path(key: str) -> str:
    return "/" + quote(key, safe="/-_.~")


def get_object(
    target: S3Target, credentials: S3Credentials, key: str, destination: Path
) -> int:
    response = _request(target, credentials, "GET", key_path(key))
    written = 0
    with destination.open("wb") as handle:
        while True:
            chunk = response.read(COPY_CHUNK)
            if not chunk:
                break
            handle.write(chunk)
            written += len(chunk)
    response.close()
    return written


def put_object(
    target: S3Target,
    credentials: S3Credentials,
    key: str,
    source: Path,
    *,
    content_type: str = "",
) -> None:
    headers = {"Content-Type": content_type} if content_type else {}
    response = _request(
        target,
        credentials,
        "PUT",
        key_path(key),
        body=source.read_bytes(),
        headers=headers,
    )
    response.read()
    response.close()


def delete_object(target: S3Target, credentials: S3Credentials, key: str) -> None:
    response = _request(target, credentials, "DELETE", key_path(key))
    response.read()
    response.close()
