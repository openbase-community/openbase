"""Isolate unit tests from installed state, saved credentials, and Cloud.

Redirect import-time paths before collection, reset credentials per test,
and fail external Python socket calls even when a test forgets a mock.
Loopback remains available for test-owned fixture servers. Subprocesses
and native clients still require explicit mocks; this is not an OS sandbox.
"""

import ipaddress
import os
import socket
import tempfile

import pytest

# Set this before collecting test modules: many runtime paths are computed at
# import time, and a per-test fixture is too late to protect those imports.
_test_data_dir = tempfile.TemporaryDirectory(prefix="openbase-unit-tests-")
_original_data_dir = os.environ.get("OPENBASE_CODER_CLI_DATA_DIR")
os.environ["OPENBASE_CODER_CLI_DATA_DIR"] = _test_data_dir.name
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
    if _original_data_dir is None:
        os.environ.pop("OPENBASE_CODER_CLI_DATA_DIR", None)
    else:
        os.environ["OPENBASE_CODER_CLI_DATA_DIR"] = _original_data_dir
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
    monkeypatch.setattr(
        "openbase_coder_cli.code_sync.conflicts.CODE_SYNC_CONFLICTS_PATH",
        tmp_path / "code-sync-conflicts.json",
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
