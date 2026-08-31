from __future__ import annotations

import os
import socket
import subprocess
import uuid
from pathlib import Path

import pytest

from openbase_coder_cli import codex_control_plane


def _short_socket_path(label: str) -> Path:
    return Path("/tmp") / f"ob-{os.getpid()}-{uuid.uuid4().hex[:8]}-{label}.sock"


def test_managed_endpoint_migrates_legacy_default_on_unix(tmp_path: Path) -> None:
    endpoint = codex_control_plane.managed_codex_app_server_endpoint(
        {
            "CODEX_HOME": str(tmp_path / "codex"),
            "CODEX_APP_SERVER_URL": "ws://127.0.0.1:4500",
        },
        platform="darwin",
    )

    assert endpoint.value == "unix://"
    assert endpoint.socket_path == (
        tmp_path / "codex" / "app-server-control" / "app-server-control.sock"
    )


def test_managed_endpoint_preserves_custom_websocket_and_windows_default() -> None:
    custom = codex_control_plane.managed_codex_app_server_endpoint(
        {"CODEX_APP_SERVER_URL": "wss://codex.example/rpc"},
        platform="linux",
    )
    windows = codex_control_plane.managed_codex_app_server_endpoint(
        {},
        platform="win32",
    )

    assert custom.value == "wss://codex.example/rpc"
    assert windows.value == "ws://127.0.0.1:4500"


def test_stale_socket_is_recovered_without_removing_live_owner(tmp_path: Path) -> None:
    stale_path = _short_socket_path("stale")
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(stale_path))
    stale.close()

    assert codex_control_plane.recover_stale_codex_control_socket(stale_path) is True
    assert not stale_path.exists()

    live_path = _short_socket_path("live")
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(live_path))
    live.listen()
    try:
        with pytest.raises(RuntimeError, match="live owner"):
            codex_control_plane.recover_stale_codex_control_socket(live_path)
        assert live_path.exists()
    finally:
        live.close()
        live_path.unlink(missing_ok=True)


def test_non_socket_control_path_is_never_replaced(tmp_path: Path) -> None:
    path = tmp_path / "control.sock"
    path.write_text("keep", encoding="utf-8")

    with pytest.raises(RuntimeError, match="not a Unix socket"):
        codex_control_plane.recover_stale_codex_control_socket(path)

    assert path.read_text(encoding="utf-8") == "keep"


def test_codex_unix_prerequisite_is_actionable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        codex_control_plane.subprocess,
        "run",
        lambda *_args, **_kwargs: subprocess.CompletedProcess(
            [], 0, stdout="--listen ws://", stderr=""
        ),
    )

    with pytest.raises(RuntimeError, match="Codex 0.151.0 or newer"):
        codex_control_plane.require_codex_unix_control_socket("codex")


def test_unix_start_refuses_legacy_competing_owner(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    endpoint = codex_control_plane.managed_codex_app_server_endpoint(
        {"CODEX_HOME": str(tmp_path / "codex")},
        platform="linux",
    )
    monkeypatch.setattr(
        codex_control_plane, "require_codex_unix_control_socket", lambda _binary: None
    )
    monkeypatch.setattr(codex_control_plane, "_legacy_app_server_ready", lambda: True)

    with pytest.raises(RuntimeError, match="legacy Codex app-server owner"):
        codex_control_plane.prepare_codex_app_server_start(endpoint, "codex")
