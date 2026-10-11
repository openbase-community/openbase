from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from openbase_coder_cli import linux_computer_use as lcu


@pytest.fixture
def toolchain(monkeypatch):
    monkeypatch.setattr(lcu.shutil, "which", lambda command: f"/usr/bin/{command}")


def test_desktop_maps_remote_control_to_xdotool(toolchain):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    desktop = lcu.LinuxDesktop(display=":7", runner=fake_run)

    desktop.handle_remote_control_message(
        {"action": "move", "deltaX": 10, "deltaY": -5}
    )
    desktop.handle_remote_control_message({"action": "click", "button": "right"})
    desktop.handle_remote_control_message({"action": "type", "text": "hello"})
    desktop.handle_remote_control_message({"action": "keypress", "keys": ["CTRL", "A"]})
    desktop.handle_remote_control_message(
        {"action": "keypress", "keys": ["COMMAND", "C"]}
    )

    assert commands == [
        ["xdotool", "mousemove_relative", "--", "14", "-7"],
        ["xdotool", "click", "3"],
        ["xdotool", "type", "--clearmodifiers", "--delay", "0", "hello"],
        ["xdotool", "key", "--clearmodifiers", "ctrl+a"],
        ["xdotool", "key", "--clearmodifiers", "ctrl+c"],
    ]


def test_desktop_maps_openai_actions_to_xdotool(toolchain):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    desktop = lcu.LinuxDesktop(display=":7", runner=fake_run)

    desktop.execute_openai_action(
        {"type": "click", "x": 12.4, "y": 50.2, "button": "left"}
    )
    desktop.execute_openai_action(
        {"type": "drag", "path": [{"x": 1, "y": 2}, {"x": 3, "y": 4}]}
    )

    assert commands == [
        ["xdotool", "mousemove", "12", "50"],
        ["xdotool", "click", "--repeat", "1", "1"],
        ["xdotool", "mousemove", "1", "2"],
        ["xdotool", "mousedown", "1"],
        ["xdotool", "mousemove", "3", "4"],
        ["xdotool", "mouseup", "1"],
    ]


def test_screenshot_rgba_uses_scrot_identify_and_convert(tmp_path, toolchain):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[0] == "scrot":
            Path(command[1]).write_bytes(b"png")
            return subprocess.CompletedProcess(command, 0, stdout="")
        if command[0] == "identify":
            return subprocess.CompletedProcess(command, 0, stdout="10 20")
        if command[0] == "convert":
            return subprocess.CompletedProcess(command, 0, stdout=b"rgba")
        return subprocess.CompletedProcess(command, 0, stdout="")

    desktop = lcu.LinuxDesktop(display=":7", runner=fake_run)

    raw, width, height = desktop.screenshot_rgba()

    assert (raw, width, height) == (b"rgba", 10, 20)
    assert [command[0] for command in commands] == ["scrot", "identify", "convert"]


def test_require_ready_reports_missing_tools(monkeypatch):
    monkeypatch.setattr(lcu.shutil, "which", lambda _command: None)
    desktop = lcu.LinuxDesktop(display="", runner=subprocess.run)

    with pytest.raises(lcu.LinuxComputerUseError) as exc:
        desktop.require_ready()

    assert "DISPLAY" in str(exc.value)
    assert "xdotool" in str(exc.value)


@pytest.fixture
def no_display_env(monkeypatch):
    monkeypatch.delenv("OPENBASE_COMPUTER_USE_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("XAUTHORITY", raising=False)


def test_detect_display_prefers_environment(monkeypatch, no_display_env):
    monkeypatch.setenv("DISPLAY", ":3")
    assert lcu.detect_display() == ":3"

    monkeypatch.setenv("OPENBASE_COMPUTER_USE_DISPLAY", ":9")
    assert lcu.detect_display() == ":9"


def test_detect_display_finds_user_owned_x_socket(
    monkeypatch, tmp_path, no_display_env
):
    for name in ("X1", "X5", "Xabc", "other"):
        (tmp_path / name).touch()
    monkeypatch.setattr(lcu, "X11_SOCKET_DIR", tmp_path)

    assert lcu.detect_display() == ":1"


def test_detect_display_ignores_other_users_sockets(
    monkeypatch, tmp_path, no_display_env
):
    (tmp_path / "X0").touch()
    monkeypatch.setattr(lcu, "X11_SOCKET_DIR", tmp_path)
    monkeypatch.setattr(lcu.os, "getuid", lambda: 999999999)

    assert lcu.detect_display() == lcu.DEFAULT_DISPLAY


def test_detect_xauthority_prefers_environment_then_dcv(
    monkeypatch, tmp_path, no_display_env
):
    monkeypatch.setattr(lcu, "DCV_XAUTH_DIR_TEMPLATE", str(tmp_path / "{uid}"))
    dcv_dir = tmp_path / str(lcu.os.getuid())
    dcv_dir.mkdir()
    assert lcu.detect_xauthority() is None

    xauth_path = dcv_dir / "openbase.xauth"
    xauth_path.touch()
    assert lcu.detect_xauthority() == str(xauth_path)

    monkeypatch.setenv("XAUTHORITY", "/custom/xauth")
    assert lcu.detect_xauthority() == "/custom/xauth"


def test_run_exports_display_and_xauthority(toolchain):
    captured_envs: list[dict] = []

    def fake_run(command, **kwargs):
        captured_envs.append(kwargs["env"])
        return subprocess.CompletedProcess(command, 0, stdout="")

    desktop = lcu.LinuxDesktop(
        display=":7", xauthority="/run/user/1/dcv/s.xauth", runner=fake_run
    )
    desktop.click_current()

    assert captured_envs[0]["DISPLAY"] == ":7"
    assert captured_envs[0]["XAUTHORITY"] == "/run/user/1/dcv/s.xauth"


def _sharing_companion(desktop: lcu.LinuxDesktop) -> lcu.LinuxCompanion:
    companion = lcu.LinuxCompanion(desktop=desktop)
    companion.state = "sharing"
    companion._room = object()
    return companion


def test_desktop_control_requires_screen_share(toolchain):
    desktop = lcu.LinuxDesktop(display=":7", runner=subprocess.run)
    companion = lcu.LinuxCompanion(desktop=desktop)

    with pytest.raises(lcu.LinuxComputerUseError, match="Screen sharing is not active"):
        companion.desktop_control_screenshot()
    with pytest.raises(lcu.LinuxComputerUseError, match="Screen sharing is not active"):
        companion.desktop_control_action({"type": "left_click", "x": 1, "y": 2})


def test_desktop_control_screenshot_downscales_and_records_dims(tmp_path, toolchain):
    def fake_run(command, **kwargs):
        if command[0] == "scrot":
            Path(command[1]).write_bytes(b"native-png")
            return subprocess.CompletedProcess(command, 0, stdout="")
        if command[0] == "identify" and "native.png" in command[-1]:
            return subprocess.CompletedProcess(command, 0, stdout="1372 890")
        if command[0] == "identify":
            return subprocess.CompletedProcess(command, 0, stdout="2744 1780")
        if command[0] == "convert":
            Path(command[-1]).write_bytes(b"scaled-png")
            return subprocess.CompletedProcess(command, 0, stdout="")
        return subprocess.CompletedProcess(command, 0, stdout="")

    # First identify call reports the native capture as 2744x1780; the convert
    # output is identified as 1372x890.
    calls = {"identify": 0}

    def dispatching_run(command, **kwargs):
        if command[0] == "identify":
            calls["identify"] += 1
            size = "2744 1780" if calls["identify"] == 1 else "1372 890"
            return subprocess.CompletedProcess(command, 0, stdout=size)
        return fake_run(command, **kwargs)

    desktop = lcu.LinuxDesktop(display=":7", runner=dispatching_run)
    companion = _sharing_companion(desktop)

    payload = companion.desktop_control_screenshot()

    assert payload["ok"] is True
    assert (payload["width"], payload["height"]) == (1372, 890)
    assert companion._mcp_screenshot_dims == (1372, 890, 2744, 1780)


def test_desktop_control_action_scales_to_native_pixels(toolchain):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="")

    desktop = lcu.LinuxDesktop(display=":7", runner=fake_run)
    companion = _sharing_companion(desktop)
    companion._mcp_screenshot_dims = (1372, 890, 2744, 1780)

    companion.desktop_control_action({"type": "left_click", "x": 686, "y": 445})
    companion.desktop_control_action({"type": "key", "combo": "cmd+n"})

    assert commands == [
        ["xdotool", "mousemove", "1372", "890"],
        ["xdotool", "click", "--repeat", "1", "1"],
        ["xdotool", "key", "--clearmodifiers", "ctrl+n"],
    ]


def test_desktop_control_cursor_maps_to_screenshot_space(toolchain):
    def fake_run(command, **kwargs):
        if command[:2] == ["xdotool", "getmouselocation"]:
            return subprocess.CompletedProcess(command, 0, stdout="X=1372\nY=890\n")
        return subprocess.CompletedProcess(command, 0, stdout="")

    desktop = lcu.LinuxDesktop(display=":7", runner=fake_run)
    companion = _sharing_companion(desktop)
    companion._mcp_screenshot_dims = (1372, 890, 2744, 1780)

    payload = companion.desktop_control_cursor()

    assert payload == {"ok": True, "x": 686, "y": 445}


def test_desktop_control_open_app_activates_existing_window(toolchain):
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["xdotool", "search"]:
            return subprocess.CompletedProcess(command, 0, stdout="41\n42\n")
        return subprocess.CompletedProcess(command, 0, stdout="")

    desktop = lcu.LinuxDesktop(display=":7", runner=fake_run)
    companion = _sharing_companion(desktop)

    payload = companion.desktop_control_open_app({"name": "Files"})

    assert payload["ok"] is True
    assert ["xdotool", "windowactivate", "42"] in commands


class _FakeRoom:
    def __init__(self, participants):
        self.remote_participants = {p.identity: p for p in participants}
        self.disconnected = False

    async def disconnect(self):
        self.disconnected = True


def _participant(identity, kind):
    from types import SimpleNamespace

    return SimpleNamespace(identity=identity, kind=kind)


def _viewer():
    from livekit import rtc

    return _participant(
        "gabe@example.com", rtc.ParticipantKind.PARTICIPANT_KIND_STANDARD
    )


def _agent():
    from livekit import rtc

    return _participant("agent-dispatcher", rtc.ParticipantKind.PARTICIPANT_KIND_AGENT)


def _companion_with_room(room) -> lcu.LinuxCompanion:
    companion = lcu.LinuxCompanion(desktop=lcu.LinuxDesktop(display=":7"))
    companion.state = "sharing"
    companion._room = room
    companion._remote_control_enabled = True
    companion._authorized_identity = "gabe@example.com"
    return companion


def _wait_for(predicate, timeout=2.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _assert_share_ended(companion, room):
    assert _wait_for(lambda: companion.state == "off")
    assert room.disconnected
    assert companion._room is None
    assert companion._remote_control_enabled is False
    assert companion._authorized_identity is None


def test_viewer_stop_message_ends_share():
    import json
    from types import SimpleNamespace

    room = _FakeRoom([_viewer(), _agent()])
    companion = _companion_with_room(room)
    packet = SimpleNamespace(
        data=json.dumps({"type": "openbase.screen_share.stop"}).encode(),
        participant=_viewer(),
    )

    companion._loop.call_soon_threadsafe(companion._handle_data_packet, packet)

    _assert_share_ended(companion, room)


def test_agent_cannot_end_share_with_stop_message():
    import json
    from types import SimpleNamespace

    room = _FakeRoom([_viewer(), _agent()])
    companion = _companion_with_room(room)
    packet = SimpleNamespace(
        data=json.dumps({"type": "openbase.screen_share.stop"}).encode(),
        participant=_agent(),
    )

    companion._loop.call_soon_threadsafe(companion._handle_data_packet, packet)

    assert not _wait_for(lambda: companion.state == "off", timeout=0.3)
    assert room.disconnected is False


def test_share_ends_when_only_agents_remain(monkeypatch):
    monkeypatch.setattr(lcu, "VIEWERLESS_GRACE_SECONDS", 0.05)
    # The phone hung up, backgrounded or dropped: only the voice agent is left.
    room = _FakeRoom([_agent()])
    companion = _companion_with_room(room)

    companion._loop.call_soon_threadsafe(companion._evaluate_viewers)

    _assert_share_ended(companion, room)


def test_returning_viewer_cancels_the_timeout(monkeypatch):
    monkeypatch.setattr(lcu, "VIEWERLESS_GRACE_SECONDS", 0.2)
    room = _FakeRoom([_agent()])
    companion = _companion_with_room(room)

    companion._loop.call_soon_threadsafe(companion._evaluate_viewers)
    viewer = _viewer()
    room.remote_participants[viewer.identity] = viewer
    companion._loop.call_soon_threadsafe(companion._evaluate_viewers)

    assert not _wait_for(lambda: companion.state == "off", timeout=0.5)
    assert room.disconnected is False


def test_room_disconnect_ends_share():
    room = _FakeRoom([_viewer()])
    companion = _companion_with_room(room)

    companion._loop.call_soon_threadsafe(
        companion._schedule_end_share, "room_disconnected"
    )

    _assert_share_ended(companion, room)
