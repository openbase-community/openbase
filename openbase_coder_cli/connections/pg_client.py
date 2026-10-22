"""Simple-query protocol client used by ``openbase-coder db query`` and the
read-only role management: runs SQL on an authenticated upstream connection
and returns text-format rows. Only the simple query protocol is used, so
values arrive as text exactly as psql would print them."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .pg_protocol import (
    UpstreamError,
    UpstreamTarget,
    encode_message,
    open_authenticated,
    parse_error_fields,
    read_message,
)


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[list[str | None]] = field(default_factory=list)
    status: str = ""


@dataclass
class QueryOutcome:
    results: list[QueryResult]
    notices: list[str]


def _drain_until_ready(sock) -> None:
    while True:
        kind, payload = read_message(sock)
        if kind == b"Z":
            return
        if kind == b"E":
            raise UpstreamError(parse_error_fields(payload))


def _parse_row_description(payload: bytes) -> list[str]:
    (count,) = struct.unpack("!H", payload[:2])
    offset = 2
    names: list[str] = []
    for _ in range(count):
        end = payload.index(b"\0", offset)
        names.append(payload[offset:end].decode("utf-8", "replace"))
        offset = (
            end + 1 + 18
        )  # tableoid(4) attnum(2) typoid(4) typlen(2) typmod(4) format(2)
    return names


def _parse_data_row(payload: bytes) -> list[str | None]:
    (count,) = struct.unpack("!H", payload[:2])
    offset = 2
    values: list[str | None] = []
    for _ in range(count):
        (length,) = struct.unpack("!i", payload[offset : offset + 4])
        offset += 4
        if length < 0:
            values.append(None)
            continue
        values.append(payload[offset : offset + length].decode("utf-8", "replace"))
        offset += length
    return values


def run_sql(target: UpstreamTarget, sql: str, *, timeout: float = 60.0) -> QueryOutcome:
    """Execute ``sql`` (one or more statements) and collect every result set.

    An ErrorResponse raises ``UpstreamError`` after the server's ReadyForQuery
    has been consumed, so the error carries Postgres' own message.
    """
    sock = open_authenticated(target, timeout=timeout)
    try:
        sock.settimeout(timeout)
        _drain_until_ready(sock)
        sock.sendall(encode_message(b"Q", sql.encode("utf-8") + b"\0"))
        results: list[QueryResult] = []
        notices: list[str] = []
        current: QueryResult | None = None
        error: UpstreamError | None = None
        while True:
            kind, payload = read_message(sock)
            if kind == b"T":
                current = QueryResult(columns=_parse_row_description(payload))
            elif kind == b"D":
                if current is None:
                    current = QueryResult()
                current.rows.append(_parse_data_row(payload))
            elif kind == b"C":
                status = payload.rstrip(b"\0").decode("utf-8", "replace")
                if current is None:
                    current = QueryResult()
                current.status = status
                results.append(current)
                current = None
            elif kind == b"I":
                results.append(QueryResult(status="EMPTY QUERY"))
            elif kind == b"E":
                error = UpstreamError(parse_error_fields(payload))
            elif kind == b"N":
                notices.append(parse_error_fields(payload).get("M", ""))
            elif kind == b"Z":
                break
            # ParameterStatus and other asynchronous messages are ignored.
        if error is not None:
            raise error
        return QueryOutcome(results=results, notices=notices)
    finally:
        try:
            sock.sendall(encode_message(b"X", b""))
        except OSError:
            pass
        sock.close()


def quote_identifier(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    # Standard-conforming strings: only the quote needs doubling.
    return "'" + value.replace("'", "''") + "'"
