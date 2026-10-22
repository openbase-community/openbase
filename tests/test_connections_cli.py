"""`openbase-coder db` / `storage` command plumbing with Cloud mocked: URL
parsing, vault calls, output formatting, local proxy state and the Cloud
client's error mapping."""

from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from openbase_coder_cli.cli.db import db, parse_postgres_url
from openbase_coder_cli.cli.storage import storage
from openbase_coder_cli.connections import cloud, commands, state
from openbase_coder_cli.services import cloud_registration


@pytest.fixture(autouse=True)
def local_state(monkeypatch, tmp_path):
    monkeypatch.setattr(state, "CONNECTIONS_DIR", tmp_path / "connections")
    monkeypatch.setattr(
        state, "PROXIES_JSON_PATH", tmp_path / "connections" / "proxies.json"
    )
    monkeypatch.setattr(state, "AUDIT_LOG_PATH", tmp_path / "connections" / "audit.log")
    monkeypatch.setattr(commands, "device_label", lambda: "test-mac")


def test_parse_postgres_url_extracts_target_and_credentials():
    target, credentials = parse_postgres_url(
        "postgresql://app%40user:p%40ss@db.example.test:6543/shop?sslmode=verify-full"
    )
    assert target == {
        "host": "db.example.test",
        "port": 6543,
        "database": "shop",
        "ssl_mode": "verify-full",
    }
    assert credentials == {"user": "app@user", "password": "p@ss"}
    target, _ = parse_postgres_url("postgres://u:p@localhost/db")
    assert (target["port"], target["ssl_mode"]) == (5432, "require")


@pytest.mark.parametrize(
    "url",
    [
        "mysql://u:p@h/db",
        "postgresql://u:p@/db",
        "postgresql://u:p@h/",
        "postgresql://u:p@h/db?sslmode=bogus",
    ],
)
def test_parse_postgres_url_rejects_bad_urls(url):
    import click

    with pytest.raises(click.ClickException):
        parse_postgres_url(url)


def test_db_add_sends_structured_payload_and_never_echoes_the_password(monkeypatch):
    calls = []

    def fake_add(kind, name, target, credentials):
        calls.append((kind, name, target, credentials))
        return {"id": "c1", "roles": list(credentials)}

    monkeypatch.setattr(cloud, "add_connection", fake_add)
    monkeypatch.setenv(
        "OPENBASE_DATABASE_URL", "postgresql://owner:hunter2@db.example.test/shop"
    )
    result = CliRunner().invoke(db, ["add", "shop"])
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "postgres",
            "shop",
            {
                "host": "db.example.test",
                "port": 5432,
                "database": "shop",
                "ssl_mode": "require",
            },
            {"readwrite": {"user": "owner", "password": "hunter2"}},
        )
    ]
    assert "hunter2" not in result.output
    assert "db proxy shop" in result.output
    audit_lines = [
        json.loads(line) for line in state.AUDIT_LOG_PATH.read_text().splitlines()
    ]
    assert audit_lines[0]["action"] == "added" and "hunter2" not in json.dumps(
        audit_lines
    )


def test_db_list_shows_roles_revocation_and_local_proxies(monkeypatch):
    monkeypatch.setattr(
        cloud,
        "list_connections",
        lambda kind=None: [
            {
                "id": "c1",
                "name": "shop",
                "provider": "neon",
                "roles": ["readwrite", "readonly"],
                "revoked_at": None,
                "target": {"host": "h", "port": 5432, "database": "shop"},
            },
            {
                "id": "c2",
                "name": "old",
                "provider": "external",
                "roles": ["readwrite"],
                "revoked_at": "2026-10-22T00:00:00Z",
                "target": {"host": "h2", "port": 5432, "database": "old"},
            },
        ],
    )
    state.register_proxy(
        "postgres", "shop", 5433, "readwrite", "postgresql://127.0.0.1:5433/shop"
    )
    result = CliRunner().invoke(db, ["list"])
    assert result.exit_code == 0, result.output
    assert (
        "shop" in result.output
        and "proxy postgresql://127.0.0.1:5433/shop" in result.output
    )
    assert "old" in result.output and "REVOKED" in result.output
    result = CliRunner().invoke(db, ["list", "--json"])
    payload = json.loads(result.output)
    assert payload[0]["proxy"]["port"] == 5433 and payload[1]["proxy"] is None


def test_db_query_leases_the_requested_role_and_formats_rows(monkeypatch):
    leases = []

    def fake_find(kind, name):
        return {
            "id": "c1",
            "name": name,
            "roles": ["readwrite", "readonly"],
            "revoked_at": None,
        }

    def fake_lease(connection_id, *, role, purpose, device):
        leases.append((connection_id, role, purpose, device))
        return {
            "id": connection_id,
            "role": role,
            "target": {
                "host": "h",
                "port": 5432,
                "database": "shop",
                "ssl_mode": "disable",
            },
            "credentials": {"user": "ro", "password": "secret"},
        }

    import sys

    db_module = sys.modules["openbase_coder_cli.cli.db"]
    from openbase_coder_cli.connections.pg_client import QueryOutcome, QueryResult

    seen_targets = []

    def fake_run_sql(target, sql):
        seen_targets.append((target, sql))
        return QueryOutcome(
            results=[
                QueryResult(
                    columns=["id", "name"],
                    rows=[["1", "a"], ["2", None]],
                    status="SELECT 2",
                )
            ],
            notices=[],
        )

    monkeypatch.setattr(cloud, "find_connection", fake_find)
    monkeypatch.setattr(cloud, "lease_connection", fake_lease)
    monkeypatch.setattr(db_module, "run_sql", fake_run_sql)
    result = CliRunner().invoke(db, ["query", "shop", "--read-only", "select * from t"])
    assert result.exit_code == 0, result.output
    assert leases == [("c1", "readonly", "db query", "test-mac")]
    assert seen_targets[0][0].user == "ro" and seen_targets[0][0].password == "secret"
    assert "id | name" in result.output and "(2 rows)" in result.output
    assert "secret" not in result.output
    result = CliRunner().invoke(db, ["query", "shop", "--json", "select 1"])
    assert json.loads(result.output)[0]["rows"] == [
        {"id": "1", "name": "a"},
        {"id": "2", "name": None},
    ]


def test_db_query_refuses_missing_role_and_revoked_connections(monkeypatch):
    monkeypatch.setattr(
        cloud,
        "find_connection",
        lambda kind, name: {
            "id": "c1",
            "name": name,
            "roles": ["readwrite"],
            "revoked_at": None,
        },
    )
    result = CliRunner().invoke(db, ["query", "shop", "--read-only", "select 1"])
    assert result.exit_code != 0 and "no readonly credentials" in result.output
    monkeypatch.setattr(
        cloud,
        "find_connection",
        lambda kind, name: {
            "id": "c1",
            "name": name,
            "roles": ["readwrite"],
            "revoked_at": "now",
        },
    )
    result = CliRunner().invoke(db, ["query", "shop", "select 1"])
    assert (
        result.exit_code != 0
        and "revoked" in result.output
        and "db restore shop" in result.output
    )


def test_storage_add_reads_keys_from_the_environment(monkeypatch):
    calls = []
    monkeypatch.setattr(
        cloud,
        "add_connection",
        lambda kind, name, target, credentials: (
            calls.append((kind, name, target, credentials))
            or {"roles": list(credentials)}
        ),
    )
    monkeypatch.setenv("OPENBASE_STORAGE_ACCESS_KEY_ID", "AKRW")
    monkeypatch.setenv("OPENBASE_STORAGE_SECRET_ACCESS_KEY", "rw-secret")
    monkeypatch.setenv("OPENBASE_STORAGE_READONLY_ACCESS_KEY_ID", "AKRO")
    monkeypatch.setenv("OPENBASE_STORAGE_READONLY_SECRET_ACCESS_KEY", "ro-secret")
    result = CliRunner().invoke(
        storage,
        [
            "add",
            "uploads",
            "--endpoint",
            "https://s3.example.test/",
            "--bucket",
            "b",
            "--region",
            "us-east-005",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        (
            "s3",
            "uploads",
            {
                "endpoint": "https://s3.example.test",
                "bucket": "b",
                "region": "us-east-005",
                "path_style": True,
            },
            {
                "readwrite": {
                    "access_key_id": "AKRW",
                    "secret_access_key": "rw-secret",
                },
                "readonly": {"access_key_id": "AKRO", "secret_access_key": "ro-secret"},
            },
        )
    ]
    assert "rw-secret" not in result.output and "ro-secret" not in result.output


def test_proxy_registry_drops_dead_processes_and_stops_live_ones(monkeypatch):
    state.register_proxy(
        "postgres", "shop", 5433, "readwrite", "postgresql://127.0.0.1:5433/shop"
    )
    assert state.find_proxy("postgres", "shop").port == 5433
    state._save_proxies(
        [
            *state.load_proxies(),
            state.RunningProxy(
                "s3", "dead", 9000, 2**22 - 7, "readwrite", "x", "http://127.0.0.1:9000"
            ),
        ]
    )
    monkeypatch.setattr(state, "_pid_alive", lambda pid: pid != 2**22 - 7)
    assert [proxy.name for proxy in state.load_proxies()] == ["shop"]
    killed = []
    monkeypatch.setattr(state, "_pid_alive", lambda pid: not killed)
    monkeypatch.setattr(state.os, "kill", lambda pid, sig: killed.append((pid, sig)))
    assert state.stop_proxy("postgres", "shop") is True
    assert killed and state.find_proxy("postgres", "shop") is None
    assert state.stop_proxy("postgres", "shop") is False


def test_cloud_client_distinguishes_missing_feature_from_missing_connection(
    monkeypatch,
):
    class FakeTokenManager:
        def __init__(self, web_backend_url):
            pass

        def get_access_token(self):
            return "jwt.token"

    monkeypatch.setattr(cloud_registration, "TokenManager", FakeTokenManager)
    monkeypatch.setattr(
        cloud_registration, "web_backend_url", lambda: "https://cloud.example.test"
    )
    responses = {}

    def fake_request(method, url, **kwargs):
        responses["last"] = (method, url, kwargs.get("headers"))
        return httpx.Response(
            404, json={"detail": "Not found."}, request=httpx.Request(method, url)
        )

    monkeypatch.setattr(cloud_registration.httpx, "request", fake_request)
    with pytest.raises(
        cloud.ConnectionsError, match="does not offer managed connections"
    ):
        cloud.list_connections("postgres")
    assert responses["last"][1].endswith("/api/openbase/connections/?kind=postgres")
    with pytest.raises(cloud.ConnectionsError, match="no longer exists"):
        cloud.revoke_connection("c1")
    assert cloud.connection_allowed("c1") is False
