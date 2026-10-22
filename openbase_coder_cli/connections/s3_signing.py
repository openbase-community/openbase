"""AWS Signature Version 4 for S3-compatible object storage.

Used by the local storage proxy (which re-signs the agent's unsigned
requests with the leased key) and by the built-in ``storage ls/get/put/rm``
commands. Payloads are declared ``UNSIGNED-PAYLOAD``, which every
S3-compatible provider accepts over TLS and avoids buffering bodies.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
SIGNING_ALGORITHM = "AWS4-HMAC-SHA256"
# Headers that participate in the signature when present. Everything else is
# forwarded unsigned, which S3 permits.
SIGNABLE_HEADERS = ("host", "content-type", "content-md5", "range")


@dataclass(frozen=True)
class S3Credentials:
    access_key_id: str
    secret_access_key: str
    session_token: str = ""


def _uri_encode(value: str, *, keep_slash: bool) -> str:
    return quote(value, safe="/-_.~" if keep_slash else "-_.~")


def canonical_query(query: str) -> str:
    pairs: list[tuple[str, str]] = []
    for item in query.split("&") if query else []:
        if not item:
            continue
        key, _, value = item.partition("=")
        pairs.append(
            (
                _uri_encode(_unquote(key), keep_slash=False),
                _uri_encode(_unquote(value), keep_slash=False),
            )
        )
    return "&".join(f"{key}={value}" for key, value in sorted(pairs))


def _unquote(value: str) -> str:
    from urllib.parse import unquote

    return unquote(value)


def sign_headers(
    method: str,
    host: str,
    path: str,
    query: str,
    headers: dict[str, str],
    credentials: S3Credentials,
    *,
    region: str,
    service: str = "s3",
    payload_hash: str = UNSIGNED_PAYLOAD,
    now: datetime | None = None,
) -> dict[str, str]:
    """Return ``headers`` plus the SigV4 ``Authorization`` and ``x-amz-*`` headers.

    ``path`` is the already-encoded request path (``/bucket/key``) and
    ``query`` the raw query string.
    """
    now = now or datetime.now(UTC)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    signed: dict[str, str] = {
        key: value
        for key, value in headers.items()
        if key.lower() in SIGNABLE_HEADERS or key.lower().startswith("x-amz-")
    }
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_hash
    if credentials.session_token:
        signed["x-amz-security-token"] = credentials.session_token
    canonical_headers = {
        key.lower(): " ".join(value.split()) for key, value in signed.items()
    }
    signed_header_names = ";".join(sorted(canonical_headers))
    canonical_request = "\n".join(
        [
            method.upper(),
            _uri_encode(_unquote(path), keep_slash=True) or "/",
            canonical_query(query),
            "".join(
                f"{key}:{canonical_headers[key]}\n" for key in sorted(canonical_headers)
            ),
            signed_header_names,
            payload_hash,
        ]
    )
    scope = f"{date_stamp}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            SIGNING_ALGORITHM,
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ]
    )
    key = f"AWS4{credentials.secret_access_key}".encode()
    for part in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    result = {
        key: value
        for key, value in headers.items()
        if key.lower() not in ("authorization", "host")
    }
    result.update(
        {key: value for key, value in signed.items() if key.lower() != "host"}
    )
    result["Authorization"] = (
        f"{SIGNING_ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
        f"SignedHeaders={signed_header_names}, Signature={signature}"
    )
    return result
