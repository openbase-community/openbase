from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from openbase_coder_cli import claude_computer_use_mcp as mcp_shim

SECRET = "test-secret"


class _FakeDesktopControlHandler(BaseHTTPRequestHandler):
    requests: list[tuple[str, str, dict]] = []
    screen_share_active = True

    def _respond(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        if self.headers.get("X-Openbase-Desktop-Secret") != SECRET:
            self._respond(401, {"ok": False, "error": "Unauthorized"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).requests.append((self.command, self.path, body))

        if not type(self).screen_share_active:
            self._respond(
                409,
                {"ok": False, "error": "Screen sharing is not active."},
            )
            return

        if self.path == "/computer-use/screenshot":
            self._respond(
                200, {"ok": True, "image": "aGk=", "width": 1372, "height": 890}
            )
        elif self.path == "/computer-use/action":
            self._respond(200, {"ok": True})
        elif self.path == "/computer-use/open-app":
            self._respond(200, {"ok": True})
        elif self.path == "/computer-use/cursor":
            self._respond(200, {"ok": True, "x": 10, "y": 20})
        else:
            self._respond(404, {"ok": False, "error": "Unknown route"})

    def do_GET(self) -> None:  # noqa: N802
        self._handle()

    def do_POST(self) -> None:  # noqa: N802
        self._handle()

    def log_message(self, *args) -> None:
        pass


@pytest.fixture
def desktop_server(monkeypatch, tmp_path: Path):
    _FakeDesktopControlHandler.requests = []
    _FakeDesktopControlHandler.screen_share_active = True
    server = HTTPServer(("127.0.0.1", 0), _FakeDesktopControlHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    control_path = tmp_path / "desktop-control.json"
    control_path.write_text(
        json.dumps({"port": server.server_address[1], "secret": SECRET}),
        encoding="utf-8",
    )
    monkeypatch.setattr(mcp_shim, "DESKTOP_CONTROL_JSON_PATH", control_path)
    monkeypatch.setattr(mcp_shim, "_is_macos", lambda: True)

    yield _FakeDesktopControlHandler
    server.shutdown()


@pytest.mark.asyncio
async def test_screenshot_returns_image_and_summary(desktop_server) -> None:
    blocks = await mcp_shim._call_tool("screenshot", {})

    assert blocks[0].type == "image"
    assert blocks[0].data == "aGk="
    assert blocks[0].mimeType == "image/png"
    assert "1372x890" in blocks[1].text


@pytest.mark.asyncio
async def test_click_posts_action_with_coordinates(desktop_server) -> None:
    blocks = await mcp_shim._call_tool("left_click", {"x": 100, "y": 200})

    assert "Performed left_click" in blocks[0].text
    assert desktop_server.requests == [
        ("POST", "/computer-use/action", {"type": "left_click", "x": 100, "y": 200})
    ]


@pytest.mark.asyncio
async def test_batch_runs_actions_in_order(desktop_server) -> None:
    blocks = await mcp_shim._call_tool(
        "computer_batch",
        {
            "actions": [
                {"action": "left_click", "x": 1, "y": 2},
                {"action": "key", "combo": "cmd+n"},
            ]
        },
    )

    assert "2 batched actions" in blocks[0].text
    assert [item[2]["type"] for item in desktop_server.requests] == [
        "left_click",
        "key",
    ]


@pytest.mark.asyncio
async def test_open_application_and_cursor(desktop_server) -> None:
    open_blocks = await mcp_shim._call_tool("open_application", {"name": "Typora"})
    cursor_blocks = await mcp_shim._call_tool("cursor_position", {})

    assert "Typora" in open_blocks[0].text
    assert "(10, 20)" in cursor_blocks[0].text


@pytest.mark.asyncio
async def test_screen_share_inactive_error_is_surfaced(desktop_server) -> None:
    desktop_server.screen_share_active = False

    with pytest.raises(RuntimeError, match="Screen sharing is not active"):
        await mcp_shim._call_tool("left_click", {"x": 1, "y": 2})


@pytest.mark.asyncio
async def test_missing_control_file_falls_back_to_companion(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        mcp_shim, "DESKTOP_CONTROL_JSON_PATH", tmp_path / "missing.json"
    )

    # No desktop app: the endpoint resolves to the companion's own IPC (the
    # same contract Linux uses) instead of failing outright...
    endpoint = mcp_shim._endpoint()
    assert endpoint["prefix"] == "/desktop-control"
    assert "X-Openbase-Companion-Secret" in endpoint["headers"]

    # ...and only errors — with companion guidance — when the companion is
    # unreachable too.
    monkeypatch.setenv("OPENBASE_LIVEKIT_COMPANION_IPC_PORT", "1")
    with pytest.raises(RuntimeError, match="companion is not running"):
        await mcp_shim._call_tool("screenshot", {})


class _FakeLinuxCompanionHandler(_FakeDesktopControlHandler):
    """Same behavior, but on the Linux companion contract."""

    def _handle(self) -> None:
        if self.headers.get("X-Openbase-Companion-Secret") != SECRET:
            self._respond(401, {"ok": False, "error": "Unauthorized"})
            return

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else {}
        type(self).requests.append((self.command, self.path, body))

        if self.path == "/desktop-control/screenshot":
            self._respond(
                200, {"ok": True, "image": "aGk=", "width": 1372, "height": 890}
            )
        elif self.path == "/desktop-control/action":
            self._respond(200, {"ok": True})
        else:
            self._respond(404, {"ok": False, "error": "Unknown route"})


@pytest.fixture
def linux_companion_server(monkeypatch):
    _FakeLinuxCompanionHandler.requests = []
    server = HTTPServer(("127.0.0.1", 0), _FakeLinuxCompanionHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    monkeypatch.setattr(mcp_shim, "_is_macos", lambda: False)
    monkeypatch.setenv(
        "OPENBASE_LIVEKIT_COMPANION_IPC_PORT", str(server.server_address[1])
    )
    monkeypatch.setenv("OPENBASE_LIVEKIT_COMPANION_IPC_SECRET", SECRET)

    yield _FakeLinuxCompanionHandler
    server.shutdown()


@pytest.mark.asyncio
async def test_linux_routes_through_companion_contract(linux_companion_server) -> None:
    shot_blocks = await mcp_shim._call_tool("screenshot", {})
    click_blocks = await mcp_shim._call_tool("left_click", {"x": 10, "y": 20})

    assert shot_blocks[0].type == "image"
    assert "Performed left_click" in click_blocks[0].text
    assert linux_companion_server.requests == [
        ("POST", "/desktop-control/screenshot", {}),
        ("POST", "/desktop-control/action", {"type": "left_click", "x": 10, "y": 20}),
    ]


@pytest.mark.asyncio
async def test_linux_unreachable_reports_companion_hint(monkeypatch) -> None:
    monkeypatch.setattr(mcp_shim, "_is_macos", lambda: False)
    monkeypatch.setenv("OPENBASE_LIVEKIT_COMPANION_IPC_PORT", "1")
    monkeypatch.setenv("OPENBASE_LIVEKIT_COMPANION_IPC_SECRET", SECRET)

    with pytest.raises(RuntimeError, match="computer-use screen-share start"):
        await mcp_shim._call_tool("screenshot", {})
