from __future__ import annotations

# ruff: noqa: E402, I001

import json
import os
import socket
import subprocess
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


def test_client_follows_socket_pointer(fake_daemon, tmp_path):
    configured = tmp_path / "deep" / "syncd.sock"
    configured.parent.mkdir()
    (tmp_path / "deep" / "syncd.sock.path").write_text(str(fake_daemon.path) + "\n")
    assert sync_daemon.SyncDaemonClient(configured).status()["device"] == "laptop"


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


def test_config_toml_includes_anchor():
    cfg = sync_daemon.SyncDaemonConfig(
        device_id="d", sync_group="g", role="hub", pair_secret="s", anchor="edge"
    )
    text = cfg.to_toml()
    assert "[placement]" in text and 'anchor = "edge"' in text
    assert (
        'anchor = "hub"'
        in sync_daemon.SyncDaemonConfig(
            device_id="d", sync_group="g", role="hub", pair_secret="s"
        ).to_toml()
    )


def test_cli_configure_start_installs_service_with_installation_config(
    monkeypatch, tmp_path
):
    cfg = tmp_path / "config.toml"
    monkeypatch.setattr(sync_daemon, "SYNC_DAEMON_CONFIG_PATH", cfg)
    monkeypatch.setattr(sync_daemon, "default_device_id", lambda: "desktop-test")
    calls: list[tuple] = []
    from openbase_coder_cli.services import installation as config_mod
    from openbase_coder_cli.services import launchd

    monkeypatch.setattr(
        config_mod.InstallationConfig, "load", classmethod(lambda cls: "CONFIG")
    )
    monkeypatch.setattr(
        launchd, "install_service", lambda c, svc: calls.append((c, svc.name))
    )
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
            "--anchor",
            "edge",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls == [("CONFIG", sync_daemon.SYNC_DAEMON_SERVICE_NAME)]
    assert 'anchor = "edge"' in cfg.read_text()


def test_cli_install_binary_installs_all_three(monkeypatch, tmp_path):
    from openbase_coder_cli.cli import sync_daemon as cli_mod

    bin_dir = tmp_path / "bin"
    monkeypatch.setattr(cli_mod, "OPENBASE_BIN_DIR", bin_dir)
    srcs = {}
    for name in ("syncd", "ctl", "edge"):
        p = tmp_path / name
        p.write_bytes(b"#!/bin/sh\n")
        srcs[name] = p
    result = CliRunner().invoke(
        sync_daemon_cli,
        [
            "install-binary",
            str(srcs["syncd"]),
            "--ctl",
            str(srcs["ctl"]),
            "--edge",
            str(srcs["edge"]),
        ],
    )
    assert result.exit_code == 0, result.output
    for name in (
        sync_daemon.SYNC_DAEMON_BINARY_NAME,
        sync_daemon.SYNC_CTL_BINARY_NAME,
        sync_daemon.SYNC_EDGE_BINARY_NAME,
    ):
        assert (bin_dir / name).exists() and (bin_dir / name).stat().st_mode & 0o111


def test_root_id_is_path_derived(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Projects" / "friendforce" / "data").mkdir(parents=True)
    (tmp_path / "Projects" / "simula" / "data").mkdir(parents=True)
    a = sync_daemon.root_id_for_path(tmp_path / "Projects" / "friendforce" / "data")
    b = sync_daemon.root_id_for_path(tmp_path / "Projects" / "simula" / "data")
    assert a == "projects-friendforce-data" and b == "projects-simula-data"
    assert sync_daemon.root_id_for_path(tmp_path) == "home"


def test_install_executable_replaces_via_new_inode(tmp_path):
    src = tmp_path / "src"
    src.write_bytes(b"#!/bin/sh\necho v2\n")
    dest = tmp_path / "bin" / "tool"
    dest.parent.mkdir()
    dest.write_bytes(b"#!/bin/sh\necho v1\n")
    before = dest.stat().st_ino
    out = sync_daemon.install_executable(src, dest)
    assert out == dest and dest.read_bytes().endswith(b"v2\n")
    assert dest.stat().st_ino != before, (
        "destination must be a new inode, not an in-place overwrite"
    )
    assert dest.stat().st_mode & 0o111
    assert not (tmp_path / "bin" / "tool.new").exists()


def test_install_executable_codesigns_macho_on_darwin(monkeypatch, tmp_path):
    calls = []
    src = tmp_path / "src"
    src.write_bytes(b"\xcf\xfa\xed\xfeopenbase-syncd")
    dest = tmp_path / "bin" / "tool"
    monkeypatch.setattr(sync_daemon.sys, "platform", "darwin")
    monkeypatch.setattr(
        sync_daemon.subprocess,
        "run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    sync_daemon.install_executable(src, dest)

    assert calls
    args, kwargs = calls[0]
    assert args[0][:4] == ["codesign", "-s", "-", "-f"]
    assert kwargs["check"] is True


def test_install_executable_keeps_existing_binary_when_codesign_fails(
    monkeypatch, tmp_path
):
    src = tmp_path / "src"
    src.write_bytes(b"\xcf\xfa\xed\xfeopenbase-syncd")
    dest = tmp_path / "bin" / "tool"
    dest.parent.mkdir()
    dest.write_bytes(b"old")
    monkeypatch.setattr(sync_daemon.sys, "platform", "darwin")

    def fail_codesign(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "codesign")

    monkeypatch.setattr(sync_daemon.subprocess, "run", fail_codesign)

    with pytest.raises(subprocess.CalledProcessError):
        sync_daemon.install_executable(src, dest)

    assert dest.read_bytes() == b"old"


def test_link_cli_tools_prefers_package_and_never_replaces_user_files(tmp_path):
    user_bin, pkg, manual = tmp_path / "ub", tmp_path / "pkg", tmp_path / "man"
    for d in (pkg, manual):
        d.mkdir()
    (pkg / "edge").write_text("#!/bin/sh\n")
    (manual / "edge").write_text("#!/bin/sh\n")
    (manual / "openbase-sync").write_text("#!/bin/sh\n")
    user_bin.mkdir()
    linked = sync_daemon.link_cli_tools(
        user_bin=user_bin, package_bin=pkg, manual_bin=manual
    )
    assert sorted(p.name for p in linked) == ["edge", "openbase-sync"]
    import os as _os

    assert _os.readlink(user_bin / "edge") == str(pkg / "edge")
    assert _os.readlink(user_bin / "openbase-sync") == str(manual / "openbase-sync")
    # idempotent
    assert (
        sync_daemon.link_cli_tools(
            user_bin=user_bin, package_bin=pkg, manual_bin=manual
        )
        == []
    )
    # a real file of the user's is never replaced
    (user_bin / "edge").unlink()
    (user_bin / "edge").write_text("mine")
    sync_daemon.link_cli_tools(user_bin=user_bin, package_bin=pkg, manual_bin=manual)
    assert (user_bin / "edge").read_text() == "mine"


def test_fetch_sync_engine_verifies_checksum(tmp_path, monkeypatch):
    import hashlib
    import importlib.util
    import io
    import json as _json
    import tarfile

    spec = importlib.util.spec_from_file_location(
        "fetch_sync_engine",
        Path(__file__).resolve().parents[1] / "scripts" / "fetch_sync_engine.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in mod.BINARIES:
            data = b"#!/bin/sh\necho " + name.encode() + b"\n"
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    blob = buf.getvalue()
    pin = tmp_path / "pin.json"
    pin.write_text(
        _json.dumps(
            {
                "version": "9.9.9",
                "base_url": "https://example.invalid/e",
                "sha256": {"linux-arm64": hashlib.sha256(blob).hexdigest()},
            }
        )
    )

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        mod.urllib.request, "urlopen", lambda url, timeout=0: Resp(blob)
    )
    out = mod.fetch("aarch64-unknown-linux-gnu", tmp_path / "out", pin_path=pin)
    assert sorted(p.name for p in out) == sorted(mod.BINARIES)
    pin.write_text(
        _json.dumps(
            {
                "version": "9.9.9",
                "base_url": "https://example.invalid/e",
                "sha256": {"linux-arm64": "0" * 64},
            }
        )
    )
    with pytest.raises(SystemExit):
        mod.fetch("linux-arm64", tmp_path / "out2", pin_path=pin)
