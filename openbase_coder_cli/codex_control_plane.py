"""Shared Codex app-server endpoint and Unix-socket lifecycle helpers."""

from __future__ import annotations

import asyncio
import errno
import os
import socket
import stat
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

from super_agents.app_endpoint import (
    DEFAULT_WEBSOCKET_ENDPOINT,
    AppServerEndpoint,
    parse_app_server_endpoint,
)
from super_agents.app_server_client import CodexAppServerClient

LEGACY_CODEX_APP_SERVER_ENDPOINT = DEFAULT_WEBSOCKET_ENDPOINT
LEGACY_CODEX_READINESS_URL = "http://127.0.0.1:4500/readyz"
CODEX_APP_SERVER_ENDPOINT_ENV = "CODEX_APP_SERVER_URL"


def managed_codex_app_server_endpoint(
    env: dict[str, str] | None = None,
    *,
    platform: str | None = None,
) -> AppServerEndpoint:
    """Resolve the one endpoint used by all Openbase-managed local clients.

    The previous loopback default is migrated to the standard Unix endpoint
    on Unix. Non-default explicit WebSocket deployments and Windows keep their
    configured transport for compatibility.
    """
    values = env if env is not None else os.environ
    current_platform = platform or sys.platform
    configured = values.get(CODEX_APP_SERVER_ENDPOINT_ENV, "").strip()
    if current_platform != "win32" and configured in {
        "",
        LEGACY_CODEX_APP_SERVER_ENDPOINT,
    }:
        configured = "unix://"
    elif not configured:
        configured = LEGACY_CODEX_APP_SERVER_ENDPOINT
    return parse_app_server_endpoint(configured, env=values, source="openbase-managed")


def apply_managed_codex_app_server_endpoint(
    env: dict[str, str],
    *,
    platform: str | None = None,
) -> tuple[dict[str, str], AppServerEndpoint]:
    resolved = managed_codex_app_server_endpoint(env, platform=platform)
    updated = dict(env)
    updated[CODEX_APP_SERVER_ENDPOINT_ENV] = resolved.value
    return updated, resolved


def codex_cli_supports_unix_control_socket(binary: str) -> bool:
    result = subprocess.run(
        [binary, "app-server", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and "unix://" in result.stdout


def require_codex_unix_control_socket(binary: str) -> None:
    if not codex_cli_supports_unix_control_socket(binary):
        raise RuntimeError(
            "This Openbase release requires Codex 0.151.0 or newer for the "
            "standard Unix app-server control socket. Update the Codex CLI and retry."
        )


def _legacy_app_server_ready(timeout: float = 0.25) -> bool:
    try:
        with urllib.request.urlopen(
            LEGACY_CODEX_READINESS_URL, timeout=timeout
        ) as response:
            return 200 <= response.status < 300
    except (OSError, urllib.error.URLError):
        return False


def _socket_accepts_connections(path: Path, timeout: float = 0.25) -> bool:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(path))
    finally:
        client.close()
    return True


def recover_stale_codex_control_socket(path: Path) -> bool:
    """Remove only a proven-stale socket; never disturb a live or non-socket path."""
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISSOCK(mode):
        raise RuntimeError(
            f"Codex control path {path} exists but is not a Unix socket; refusing to replace it."
        )
    try:
        _socket_accepts_connections(path)
    except PermissionError as exc:
        raise RuntimeError(
            f"Permission denied checking Codex control socket {path}; refusing to remove it."
        ) from exc
    except OSError as exc:
        if exc.errno == errno.ENOENT:
            return False
        if exc.errno != errno.ECONNREFUSED:
            raise RuntimeError(
                f"Could not verify Codex control socket {path}; refusing to remove it: {exc}"
            ) from exc
        path.unlink(missing_ok=True)
        return True
    raise RuntimeError(
        f"Codex control socket {path} already has a live owner; refusing to replace or kill it."
    )


def prepare_codex_app_server_start(endpoint: AppServerEndpoint, binary: str) -> None:
    if not endpoint.is_unix:
        return
    require_codex_unix_control_socket(binary)
    if _legacy_app_server_ready():
        raise RuntimeError(
            "A legacy Codex app-server owner is still ready on ws://127.0.0.1:4500. "
            "Stop it before starting the standard Unix-socket owner."
        )
    assert endpoint.socket_path is not None
    recover_stale_codex_control_socket(endpoint.socket_path)


def cleanup_stale_codex_app_server_socket(endpoint: AppServerEndpoint) -> bool:
    if not endpoint.is_unix or endpoint.socket_path is None:
        return False
    try:
        return recover_stale_codex_control_socket(endpoint.socket_path)
    except RuntimeError:
        # A live owner or an unverifiable path never belongs to cleanup.
        return False


async def _codex_app_server_ready_async(endpoint: AppServerEndpoint) -> bool:
    connection_endpoint = (
        f"unix://{endpoint.socket_path}"
        if endpoint.is_unix and endpoint.socket_path is not None
        else endpoint.value
    )
    client = CodexAppServerClient(endpoint=connection_endpoint)
    try:
        return await client.check_ready()
    finally:
        await client.close()


def codex_app_server_ready(
    endpoint: AppServerEndpoint | None = None,
) -> bool:
    resolved = endpoint or managed_codex_app_server_endpoint()
    try:
        return asyncio.run(_codex_app_server_ready_async(resolved))
    except (OSError, RuntimeError):
        return False
