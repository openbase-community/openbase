"""Bounded-memory update transfers with explicit transient failure semantics."""

from __future__ import annotations

import hashlib
import http.client
import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO


class SelfUpdateError(RuntimeError):
    pass


class RetryableUpdateError(SelfUpdateError):
    """Preparation failed before activation; an automatic worker may retry."""


def _network_error(
    url: str, exc: OSError | http.client.HTTPException
) -> SelfUpdateError:
    retryable = not isinstance(exc, urllib.error.HTTPError) or (
        exc.code in (408, 429) or exc.code >= 500
    )
    kind = RetryableUpdateError if retryable else SelfUpdateError
    return kind(f"Could not fetch {url}: {exc}")


def _transfer(url: str, destination: BinaryIO, *, timeout: float) -> str:
    try:
        response = urllib.request.urlopen(url, timeout=timeout)
    except (OSError, http.client.HTTPException) as exc:
        raise _network_error(url, exc) from exc
    digest = hashlib.sha256()
    received = 0
    with response:
        expected = response.headers.get("Content-Length")
        while True:
            try:
                block = response.read(1024 * 1024)
            except (OSError, http.client.HTTPException) as exc:
                raise _network_error(url, exc) from exc
            if not block:
                break
            # Local write errors (e.g. ENOSPC) are not network failures.
            destination.write(block)
            digest.update(block)
            received += len(block)
    if expected is not None and received != int(expected):
        raise RetryableUpdateError(
            f"Incomplete download from {url}: received {received} of {expected} bytes."
        )
    return digest.hexdigest()


def fetch_bytes(url: str, *, timeout: float) -> bytes:
    destination = io.BytesIO()
    _transfer(url, destination, timeout=timeout)
    return destination.getvalue()


def download_file(url: str, destination: Path, *, sha256: str, timeout: float) -> None:
    if not sha256:
        raise SelfUpdateError("Manifest target entry has no checksum.")
    with destination.open("wb") as handle:
        digest = _transfer(url, handle, timeout=timeout)
    if digest != sha256:
        raise RetryableUpdateError(
            f"Downloaded artifact checksum mismatch (expected {sha256}, got {digest})."
        )
