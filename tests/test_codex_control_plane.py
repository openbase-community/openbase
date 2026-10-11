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


def test_stale_codex_daemon_link_is_removed_without_disturbing_live_owner(
    tmp_path: Path,
) -> None:
    stale_link = tmp_path / "stale.sock"
    stale_link.symlink_to(tmp_path / "missing.sock")
    assert codex_control_plane.recover_stale_codex_control_socket(stale_link) is True
    assert not stale_link.is_symlink()

    live_target = _short_socket_path("daemon")
    live = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    live.bind(str(live_target))
    live.listen()
    live_link = _short_socket_path("live-link")
    live_link.symlink_to(live_target)
    try:
        with pytest.raises(RuntimeError, match="live owner"):
            codex_control_plane.recover_stale_codex_control_socket(live_link)
        assert live_link.is_symlink()
        assert live_target.exists()
    finally:
        live.close()
        live_link.unlink(missing_ok=True)
        live_target.unlink(missing_ok=True)


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


def test_shared_daemon_is_recognized_by_its_control_link(tmp_path: Path) -> None:
    from types import SimpleNamespace

    link = tmp_path / "app-server-control.sock"
    link.symlink_to(tmp_path / "daemon.sock")
    plain = tmp_path / "own.sock"
    plain.touch()
    assert codex_control_plane.endpoint_is_shared_codex_daemon(
        SimpleNamespace(is_unix=True, socket_path=link)
    )
    assert not codex_control_plane.endpoint_is_shared_codex_daemon(
        SimpleNamespace(is_unix=True, socket_path=plain)
    )
    assert not codex_control_plane.endpoint_is_shared_codex_daemon(
        SimpleNamespace(is_unix=False, socket_path=None)
    )
    assert not codex_control_plane.endpoint_is_shared_codex_daemon("ws://x")


def test_idle_while_shared_daemon_returns_at_once_without_a_daemon(tmp_path) -> None:
    from types import SimpleNamespace

    endpoint = SimpleNamespace(is_unix=True, socket_path=tmp_path / "s.sock")
    logged: list[str] = []
    assert (
        codex_control_plane.idle_while_shared_codex_daemon(
            endpoint,
            ready=lambda _e: False,
            sleep=lambda _s: pytest.fail("must not sleep"),
            log=logged.append,
        )
        is False
    )
    assert logged == []


def test_idle_while_shared_daemon_logs_once_and_hands_over_after_misses(
    tmp_path,
) -> None:
    from types import SimpleNamespace

    endpoint = SimpleNamespace(is_unix=True, socket_path=tmp_path / "s.sock")
    # Live, live, one transient miss during the daemon's self-update swap,
    # live again, then gone for good.
    probes = iter([True, True, True, False, True, False, False, False])
    slept: list[float] = []
    logged: list[str] = []
    assert (
        codex_control_plane.idle_while_shared_codex_daemon(
            endpoint,
            ready=lambda _e: next(probes),
            sleep=slept.append,
            log=logged.append,
            poll_seconds=7.0,
            handover_polls=3,
        )
        is True
    )
    assert slept == [7.0] * 7
    assert len(logged) == 2
    assert "shared Codex daemon owns" in logged[0]
    assert "starting the Openbase-managed app-server" in logged[1]


def _serve_fake_codex_daemon(socket_path: Path, version: str):
    """A minimal managed-daemon stand-in: answers ``initialize`` like Codex."""
    import asyncio
    import json
    import threading

    import websockets

    started = threading.Event()
    stop: asyncio.Future | None = None
    loop = asyncio.new_event_loop()

    async def handler(connection):
        async for raw in connection:
            message = json.loads(raw)
            if message.get("method") == "initialize":
                await connection.send(
                    json.dumps(
                        {
                            "id": message["id"],
                            "result": {
                                "userAgent": f"codex_cli_rs/{version} (Mac OS 15; arm64)"
                            },
                        }
                    )
                )

    async def main():
        nonlocal stop
        stop = loop.create_future()
        async with websockets.unix_serve(handler, str(socket_path)):
            started.set()
            await stop

    thread = threading.Thread(target=loop.run_until_complete, args=(main(),), daemon=True)
    thread.start()
    assert started.wait(5)

    def shutdown():
        loop.call_soon_threadsafe(stop.set_result, None)
        thread.join(5)
        loop.close()

    return shutdown


def test_fake_managed_daemon_is_shared_advisory_and_idled_behind(monkeypatch) -> None:
    """End to end over a real Unix socket: the 2026-10-07 laptop state.

    Codex's daemon (0.161.0) serves the standard socket through a symlink
    while the installed CLI is 0.160.1. Openbase must see a shared daemon,
    classify the mismatch as a CLI upgrade (never a restart), and keep its
    own runner idle until the daemon goes away.
    """
    import threading

    from super_agents.app_endpoint import parse_app_server_endpoint

    from openbase_coder_cli.services import codex_version_skew as skew_module

    daemon_socket = _short_socket_path("fake-daemon")
    link = _short_socket_path("control-link")
    shutdown = _serve_fake_codex_daemon(daemon_socket, "0.161.0")
    link.symlink_to(daemon_socket)
    endpoint = parse_app_server_endpoint(f"unix://{link}", env={}, source="test")
    try:
        assert codex_control_plane.endpoint_is_shared_codex_daemon(endpoint)
        assert codex_control_plane.shared_codex_daemon_ready(endpoint)
        assert skew_module.running_codex_app_server_version(endpoint) == "0.161.0"

        monkeypatch.setattr(skew_module, "service_endpoint", lambda name: endpoint)
        skew = skew_module.service_version_skew(
            "codex-app-server", ("/opt/codex", "0.160.1")
        )
        assert skew is not None
        assert skew.shared_daemon and skew.running_is_newer
        assert not skew.restart_resolves
        assert "upgrade the Codex CLI to 0.161.0" in skew.message

        # Starting our own server would have raised and crash-looped.
        with pytest.raises(RuntimeError, match="live owner"):
            codex_control_plane.recover_stale_codex_control_socket(link)

        logged: list[str] = []
        result: list[bool] = []
        idle = threading.Thread(
            target=lambda: result.append(
                codex_control_plane.idle_while_shared_codex_daemon(
                    endpoint, log=logged.append, poll_seconds=0.05, handover_polls=2
                )
            )
        )
        idle.start()
        idle.join(1.0)
        assert idle.is_alive(), "runner must stay idle while the daemon serves"
        assert logged == [
            f"codex-app-server: the shared Codex daemon owns {link}; "
            "idling until it goes away instead of binding a second server"
        ]
    finally:
        shutdown()
        daemon_socket.unlink(missing_ok=True)
    try:
        idle.join(5.0)
        assert not idle.is_alive()
        assert result == [True]
        assert len(logged) == 2 and "starting the Openbase-managed" in logged[1]
        # The dangling link is now provably stale, so the start may replace it.
        assert codex_control_plane.recover_stale_codex_control_socket(link) is True
    finally:
        link.unlink(missing_ok=True)
