from __future__ import annotations

# ruff: noqa: E402, I001

import json
import os
import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django
from rest_framework.test import APIRequestFactory, force_authenticate

django.setup()

from click.testing import CliRunner

from openbase_coder_cli import sync_daemon
from openbase_coder_cli.cli.sync_daemon import sync_daemon_cli
from openbase_coder_cli.openbase_coder_cli_app import health_warnings, sync_daemon_api
from openbase_coder_cli.services import runners
from openbase_coder_cli.services.definitions import SERVICES, default_services


class FakeDaemon:
    """Serves the daemon's JSON-lines protocol on a unix socket."""

    def __init__(self, path: Path, responses: dict):
        self.path = path
        self.responses = responses
        self.requests: list[dict] = []
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(str(path))
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                buf = b""
                while not buf.endswith(b"\n"):
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    buf += chunk
                if not buf:
                    continue
                req = json.loads(buf)
                self.requests.append(req)
                resp = self.responses.get(
                    req["op"], {"ok": False, "error": "unknown op"}
                )
                conn.sendall((json.dumps(resp) + "\n").encode())

    def close(self):
        self._srv.close()


@pytest.fixture
def fake_daemon(tmp_path, monkeypatch):
    sock = Path("/tmp") / f"obs-test-{os.getpid()}.sock"
    if sock.exists():
        sock.unlink()
    responses = {
        "status": {
            "ok": True,
            "data": {
                "device": "laptop",
                "role": "edge",
                "peers": [{"device": "mini"}],
                "open_conflicts": 1,
                "roots": [],
            },
        },
        "metrics": {"ok": True, "data": {"local_changes": 3}},
        "conflicts": {
            "ok": True,
            "data": [{"id": 7, "path": "a.txt", "kind": "content"}],
        },
        "resolve": {"ok": True, "result": "ok"},
        "barrier": {"ok": True, "result": "ok", "lag": 0},
        "stubs": {"ok": True, "data": [{"Path": "big.bin", "Size": 123}]},
        "hydrate": {"ok": True, "data": {"requested": 1}},
    }
    daemon = FakeDaemon(sock, responses)
    monkeypatch.setattr(sync_daemon, "SYNC_DAEMON_SOCKET_PATH", sock)
    yield daemon
    daemon.close()
    sock.unlink(missing_ok=True)


def _request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    fn = {"GET": factory.get, "POST": factory.post}[method]
    request = fn(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


def test_service_definition_registered_but_not_default():
    svc = next(s for s in SERVICES if s.name == "sync-daemon")
    assert svc.install_by_default is False
    assert svc.command_template == "sync-daemon"
    assert svc.freshness_kind == "binary"
    assert "sync-daemon" not in {s.name for s in default_services()}


def test_runner_argv_points_at_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        sync_daemon, "SYNC_DAEMON_CONFIG_PATH", tmp_path / "config.toml"
    )
    argv, env = runners.build_sync_daemon(
        {"X": "1"}, {"openbase_syncd": "/opt/bin/openbase-syncd"}
    )
    assert argv == [
        "/opt/bin/openbase-syncd",
        "--config",
        str(tmp_path / "config.toml"),
    ]
    assert env == {"X": "1"}
    assert runners.RUNNERS["sync-daemon"][1] == ("openbase_syncd",)


def test_config_roundtrip(tmp_path):
    cfg = sync_daemon.SyncDaemonConfig(
        device_id="desktop-1",
        sync_group="g",
        role="edge",
        pair_secret="s3cr3t",
        roots=[{"id": "projects", "path": "/Users/x/Projects"}],
        peer_hot="mini:22100",
        peer_bulk="mini:22101",
    )
    path = sync_daemon.write_config(cfg, tmp_path / "config.toml")
    text = path.read_text()
    assert (
        'role = "edge"' in text
        and 'peer_hot = "mini:22100"' in text
        and "[[roots]]" in text
    )
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    summary = sync_daemon.read_config_summary(path)
    assert (
        summary["configured"]
        and summary["role"] == "edge"
        and summary["roots"][0]["path"] == "/Users/x/Projects"
    )
    assert "pair_secret" not in summary


def test_client_calls_and_errors(fake_daemon):
    client = sync_daemon.SyncDaemonClient(fake_daemon.path)
    assert client.status()["device"] == "laptop"
    assert client.conflicts()[0]["id"] == 7
    client.resolve(7, "b")
    assert fake_daemon.requests[-1] == {"op": "resolve", "id": 7, "choice": "b"}
    assert client.barrier("settle", path="/Users/x/Projects")["result"] == "ok"
    with pytest.raises(sync_daemon.SyncDaemonError):
        client.resolve(7, "c")
    with pytest.raises(sync_daemon.SyncDaemonError):
        sync_daemon.SyncDaemonClient(Path("/tmp/does-not-exist.sock")).status()


def test_api_proxies(fake_daemon):
    resp = sync_daemon_api.sync_daemon_status(
        _request("GET", "/api/sync/daemon/status/")
    )
    assert resp.status_code == 200 and resp.data["metrics"]["local_changes"] == 3
    resp = sync_daemon_api.sync_daemon_conflicts(
        _request("GET", "/api/sync/daemon/conflicts/")
    )
    assert resp.data["unresolved_count"] == 1
    resp = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request(
            "POST",
            "/api/sync/daemon/conflicts/resolve/",
            {"id": 7, "action": "use_remote"},
        )
    )
    assert resp.status_code == 200 and resp.data["choice"] == "b"
    resp = sync_daemon_api.sync_daemon_conflicts_resolve(
        _request("POST", "/api/sync/daemon/conflicts/resolve/", {"id": 7})
    )
    assert resp.status_code == 400
    resp = sync_daemon_api.sync_daemon_barrier(
        _request(
            "POST", "/api/sync/daemon/barrier/", {"kind": "flush", "path": "/Users/x"}
        )
    )
    assert resp.data["result"] == "ok"
    resp = sync_daemon_api.sync_daemon_hydrate(
        _request("POST", "/api/sync/daemon/hydrate/", {"path": "/Users/x/big.bin"})
    )
    assert resp.data == {"requested": 1}


def test_api_unavailable_when_daemon_down(monkeypatch):
    monkeypatch.setattr(
        sync_daemon, "SYNC_DAEMON_SOCKET_PATH", Path("/tmp/obs-missing.sock")
    )
    resp = sync_daemon_api.sync_daemon_status(
        _request("GET", "/api/sync/daemon/status/")
    )
    assert resp.status_code == 503


def test_health_warnings_when_configured(monkeypatch, tmp_path, fake_daemon):
    cfg = tmp_path / "config.toml"
    cfg.write_text('role = "edge"\n')
    monkeypatch.setattr(sync_daemon, "SYNC_DAEMON_CONFIG_PATH", cfg)
    warnings = health_warnings._sync_daemon_warnings()
    ids = {w["id"] for w in warnings}
    assert "sync-daemon-conflicts" in ids and "sync-daemon-unreachable" not in ids
    monkeypatch.setattr(
        sync_daemon, "SYNC_DAEMON_SOCKET_PATH", Path("/tmp/obs-missing.sock")
    )
    ids = {w["id"] for w in health_warnings._sync_daemon_warnings()}
    assert ids == {"sync-daemon-unreachable"}
    assert health_warnings._sync_daemon_expected() is True


def test_cli_configure_writes_config_without_starting(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(sync_daemon, "SYNC_DAEMON_CONFIG_PATH", cfg)
    monkeypatch.setattr(sync_daemon, "default_device_id", lambda: "desktop-test")
    runner = CliRunner()
    result = runner.invoke(
        sync_daemon_cli,
        [
            "configure",
            "--role",
            "hub",
            "--listen",
            "100.64.0.15",
            "--root",
            str(tmp_path),
            "--no-start",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Pair secret" in result.output
    text = cfg.read_text()
    assert 'role = "hub"' in text and 'listen_hot = "100.64.0.15:22100"' in text
    result = runner.invoke(
        sync_daemon_cli,
        ["configure", "--role", "edge", "--root", str(tmp_path), "--no-start"],
    )
    assert result.exit_code != 0 and "--peer" in result.output
