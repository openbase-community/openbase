"""Postgres wire-protocol pieces: SCRAM conformance, the auth-terminating
proxy and the simple-query client, against an in-process fake server."""

from __future__ import annotations

import socket
import struct
import threading

import pytest

from openbase_coder_cli.connections import pg_protocol
from openbase_coder_cli.connections.pg_client import run_sql
from openbase_coder_cli.connections.pg_protocol import (
    AUTH_CLEARTEXT,
    AUTH_OK,
    SSL_REQUEST_CODE,
    ScramClient,
    UpstreamError,
    UpstreamTarget,
    encode_message,
    parse_startup_params,
    read_exact,
    read_message,
    read_startup,
)
from openbase_coder_cli.connections.pg_proxy import PostgresProxy


def test_scram_sha_256_matches_rfc_7677_vector():
    client = ScramClient("pencil", nonce="rOprNGfwEbeRWgbNEkqO", username="user")
    assert client.client_first() == b"n,,n=user,r=rOprNGfwEbeRWgbNEkqO"
    server_first = (
        b"r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
        b"s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096"
    )
    assert client.client_final(server_first) == (
        b"c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,"
        b"p=dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ="
    )
    client.verify_server_final(b"v=6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=")
    with pytest.raises(pg_protocol.ProtocolError):
        client.verify_server_final(b"v=AAAA")


def test_startup_round_trip():
    payload = pg_protocol.encode_startup(
        {"user": "u", "database": "d", "application_name": "psql"}
    )
    (length,) = struct.unpack("!I", payload[:4])
    assert length == len(payload)
    assert parse_startup_params(payload[8:]) == {
        "user": "u",
        "database": "d",
        "application_name": "psql",
    }


class FakePostgres:
    """A server that speaks just enough v3 protocol: rejects TLS, requires a
    cleartext password, answers one canned query and reports errors."""

    def __init__(self, password: str = "pw"):
        self.password = password
        self.seen_users: list[str] = []
        self.seen_params: list[dict[str, str]] = []
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(8)
        self.port = self.listener.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.listener.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket):
        try:
            code, payload = read_startup(conn)
            if code == SSL_REQUEST_CODE:
                conn.sendall(b"N")
                code, payload = read_startup(conn)
            params = parse_startup_params(payload)
            self.seen_params.append(params)
            self.seen_users.append(params.get("user", ""))
            conn.sendall(encode_message(b"R", struct.pack("!I", AUTH_CLEARTEXT)))
            kind, body = read_message(conn)
            if kind != b"p" or body.rstrip(b"\0").decode() != self.password:
                conn.sendall(
                    encode_message(
                        b"E", b"SFATAL\0C28P01\0Mpassword authentication failed\0\0"
                    )
                )
                return
            conn.sendall(encode_message(b"R", struct.pack("!I", AUTH_OK)))
            conn.sendall(encode_message(b"S", b"server_version\017.0\0"))
            conn.sendall(encode_message(b"K", struct.pack("!II", 4242, 99)))
            conn.sendall(encode_message(b"Z", b"I"))
            while True:
                kind, body = read_message(conn)
                if kind == b"X":
                    return
                if kind != b"Q":
                    continue
                sql = body.rstrip(b"\0").decode()
                if "fail" in sql:
                    conn.sendall(
                        encode_message(
                            b"E", b"SERROR\0C42P01\0Mrelation does not exist\0\0"
                        )
                    )
                else:
                    row_description = struct.pack("!H", 2)
                    for name in (b"id", b"name"):
                        row_description += (
                            name + b"\0" + struct.pack("!IHIhiH", 0, 0, 25, -1, -1, 0)
                        )
                    conn.sendall(encode_message(b"T", row_description))
                    for row in ((b"1", b"alpha"), (b"2", None)):
                        data = struct.pack("!H", 2)
                        for value in row:
                            data += (
                                struct.pack("!i", -1)
                                if value is None
                                else struct.pack("!i", len(value)) + value
                            )
                        conn.sendall(encode_message(b"D", data))
                    conn.sendall(encode_message(b"C", b"SELECT 2\0"))
                conn.sendall(encode_message(b"Z", b"I"))
        except (OSError, pg_protocol.ProtocolError):
            return
        finally:
            conn.close()

    def close(self):
        self.listener.close()


@pytest.fixture
def fake_postgres():
    server = FakePostgres()
    yield server
    server.close()


def _target(port: int, **overrides) -> UpstreamTarget:
    values = {
        "host": "127.0.0.1",
        "port": port,
        "database": "app",
        "user": "owner",
        "password": "pw",
        "ssl_mode": "prefer",
    }
    values.update(overrides)
    return UpstreamTarget(**values)


def test_query_client_runs_sql_and_reports_errors(fake_postgres):
    outcome = run_sql(_target(fake_postgres.port), "select * from things")
    assert len(outcome.results) == 1
    assert outcome.results[0].columns == ["id", "name"]
    assert outcome.results[0].rows == [["1", "alpha"], ["2", None]]
    assert outcome.results[0].status == "SELECT 2"
    with pytest.raises(UpstreamError, match="relation does not exist"):
        run_sql(_target(fake_postgres.port), "select fail")
    with pytest.raises(UpstreamError, match="password authentication failed"):
        run_sql(_target(fake_postgres.port, password="wrong"), "select 1")


def test_proxy_authenticates_upstream_and_hides_the_password(fake_postgres):
    events: list[str] = []
    proxy = PostgresProxy(0, _target(fake_postgres.port), log=events.append)
    proxy.start()
    try:
        # The client offers no password and a different user; the upstream
        # sees the leased role and the client's application_name.
        outcome = run_sql(
            UpstreamTarget(
                host="127.0.0.1",
                port=proxy.port,
                database="ignored",
                user="agent",
                password="",
                ssl_mode="disable",
                startup_params={"application_name": "agent-tool"},
            ),
            "select * from things",
        )
        assert outcome.results[0].rows == [["1", "alpha"], ["2", None]]
        assert fake_postgres.seen_users == ["owner"]
        assert fake_postgres.seen_params[0]["database"] == "app"
        assert fake_postgres.seen_params[0]["application_name"] == "agent-tool"
        assert proxy.stats.accepted == 1 and proxy.stats.failed == 0
        assert any("opened as owner" in event for event in events)
    finally:
        proxy.close()


def test_proxy_reports_upstream_auth_failure_to_the_client(fake_postgres):
    proxy = PostgresProxy(0, _target(fake_postgres.port, password="wrong"))
    proxy.start()
    try:
        with pytest.raises(
            UpstreamError, match="openbase proxy: password authentication failed"
        ):
            run_sql(
                UpstreamTarget("127.0.0.1", proxy.port, "x", "agent", "", "disable"),
                "select 1",
            )
        assert proxy.stats.failed == 1
    finally:
        proxy.close()


def test_proxy_rejects_tls_on_loopback_and_closes_when_revoked(fake_postgres):
    allowed = {"value": True}
    proxy = PostgresProxy(
        0,
        _target(fake_postgres.port),
        still_allowed=lambda: allowed["value"],
        recheck_seconds=0.05,
    )
    proxy.start()
    try:
        with socket.create_connection(("127.0.0.1", proxy.port)) as client:
            client.sendall(struct.pack("!II", 8, SSL_REQUEST_CODE))
            assert read_exact(client, 1) == b"N"
        allowed["value"] = False
        proxy._closed.wait(2.0)
        assert proxy._closed.is_set()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", proxy.port), timeout=0.5).close()
    finally:
        proxy.close()
