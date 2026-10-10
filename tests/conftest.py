"""Isolate unit tests from installed state, saved credentials, and Cloud.

Redirect import-time paths before collection, reset credentials per test,
and fail external Python socket calls even when a test forgets a mock.
Loopback remains available for test-owned fixture servers. Subprocesses
and native clients still require explicit mocks; this is not an OS sandbox.
"""

import ipaddress
import socket
import tempfile
from pathlib import Path

import pytest

# Set this before collecting test modules: many runtime paths are computed at
# import time, and a per-test fixture is too late to protect those imports.
_test_data_dir = tempfile.TemporaryDirectory(prefix="openbase-unit-tests-")
_test_environment = pytest.MonkeyPatch()
_test_environment.setenv("OPENBASE_CODER_CLI_DATA_DIR", _test_data_dir.name)
# Setup also writes to the shared agent homes, outside Openbase's data dir.
# Override inherited homes before imports cache paths; deleting the temporary
# hook script alone would leave broken hook registrations in the user's config.
for variable, directory in (("CODEX_HOME", ".codex"), ("CLAUDE_CONFIG_DIR", ".claude")):
    home = Path(_test_data_dir.name) / directory
    home.mkdir()
    _test_environment.setenv(variable, str(home))
_network_guard = pytest.MonkeyPatch()


def _require_test_address(host):
    if isinstance(host, bytes):
        host = host.decode("ascii")
    if host == "localhost":
        return
    try:
        if ipaddress.ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    pytest.fail(
        "Unit tests cannot access external networks; mock the request.", pytrace=False
    )


def pytest_configure(config):
    """Block external sockets during collection as well as test execution."""
    original_resolve = socket.getaddrinfo
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto

    def resolve(host, *args, **kwargs):
        if host is not None:
            _require_test_address(host)
        return original_resolve(host, *args, **kwargs)

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            _require_test_address(address[0])
        return original_connect(sock, address)

    def connect_ex(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            _require_test_address(address[0])
        return original_connect_ex(sock, address)

    def sendto(sock, data, *args):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            _require_test_address(args[-1][0])
        return original_sendto(sock, data, *args)

    _network_guard.setattr(socket, "getaddrinfo", resolve)
    _network_guard.setattr(socket.socket, "connect", connect)
    _network_guard.setattr(socket.socket, "connect_ex", connect_ex)
    _network_guard.setattr(socket.socket, "sendto", sendto)


def pytest_unconfigure(config):
    _network_guard.undo()
    _test_environment.undo()
    _test_data_dir.cleanup()


@pytest.fixture(autouse=True)
def _isolated_credentials(monkeypatch, tmp_path):
    from openbase_coder_cli import paths
    from openbase_coder_cli.config import machine_token_manager, token_manager

    monkeypatch.setattr(token_manager, "_instance", None)

    for name in (
        "AUTH_JSON_PATH",
        "OWNER_IDENTITY_JSON_PATH",
        "MACHINE_TOKEN_JSON_PATH",
    ):
        path = tmp_path / getattr(paths, name).name
        monkeypatch.setattr(paths, name, path)
        for module in (token_manager, machine_token_manager):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, path)


@pytest.fixture
def isolated_registry(monkeypatch, tmp_path):
    from openbase_coder_cli.services import published_services

    path = tmp_path / "published-services.json"
    monkeypatch.setattr(published_services, "PUBLISHED_SERVICES_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _isolated_host_state(monkeypatch, tmp_path):
    env_path = tmp_path / "openbase-test.env"
    env_path.write_text("")
    monkeypatch.setattr("openbase_coder_cli.paths.DEFAULT_ENV_FILE_PATH", env_path)
    # Never read or dial this machine's real Openbase Sync daemon.
    monkeypatch.setattr(
        "openbase_coder_cli.sync_daemon.SYNC_DAEMON_CONFIG_PATH",
        tmp_path / "openbase-sync" / "config.toml",
    )
    monkeypatch.setattr(
        "openbase_coder_cli.sync_daemon.SYNC_DAEMON_SOCKET_PATH",
        tmp_path / "openbase-sync" / "syncd.sock",
    )
    # Discard inherited backend settings and changes made by other tests.
    # Tests must see only what they set themselves.
    monkeypatch.delenv("OPENBASE_CODER_CLI_WEB_BACKEND_URL", raising=False)
    monkeypatch.delenv("OPENBASE_CODING_BACKEND", raising=False)
    monkeypatch.delenv("OPENBASE_CODING_BACKENDS", raising=False)
    from openbase_coder_cli.agent_profiles import profile_environment

    for key in profile_environment():
        monkeypatch.delenv(key, raising=False)
    return env_path


@pytest.fixture(autouse=True)
def _livekit_agent_worker_ready(monkeypatch):
    """The room-token view refuses rooms while the agent worker is down; tests
    have no worker, so treat it as ready unless a test says otherwise."""
    import sys

    # Only patch when a test already imported the views (importing them here
    # would pull Django settings into every unrelated test).
    livekit_views = sys.modules.get("openbase_coder_cli.openbase_coder_cli_app.livekit")
    if livekit_views is not None:
        monkeypatch.setattr(livekit_views, "livekit_agent_worker_ready", lambda: True)


@pytest.fixture
def volume(tmp_path, monkeypatch):
    """A container's durable data directory (Maritime's /data/openbase) for the
    docker home-state scripts (test_container_home_state*.py)."""
    monkeypatch.delenv("SUPER_AGENTS_CLAUDE_CODE_HOME", raising=False)
    data_dir = tmp_path / "data" / "openbase"
    data_dir.mkdir(parents=True)
    return data_dir
