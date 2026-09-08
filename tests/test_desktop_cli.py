from __future__ import annotations

import importlib

import click
import pytest
from click.testing import CliRunner

from openbase_coder_cli.cli import main

desktop_cli = importlib.import_module("openbase_coder_cli.cli.desktop")


def test_desktop_screen_share_start_posts_session(monkeypatch):
    calls = []

    monkeypatch.setattr(desktop_cli.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        desktop_cli,
        "_load_companion_session",
        lambda room_name: {
            "roomUrl": "ws://livekit.example",
            "companionToken": "token-1",
            "roomName": room_name or "room-1",
        },
    )

    def fake_request(method, path, *, json=None, launch=True):
        calls.append((method, path, json, launch))
        return {"ok": True, "state": "sharing"}

    monkeypatch.setattr(desktop_cli, "_desktop_control_request", fake_request)

    result = CliRunner().invoke(
        main, ["desktop", "screen-share", "start", "--room", "room-2"]
    )

    assert result.exit_code == 0
    assert "Desktop screen share started (sharing)." in result.output
    assert calls == [
        (
            "POST",
            "/livekit-companion/start-screen-share",
            {
                "roomUrl": "ws://livekit.example",
                "companionToken": "token-1",
                "roomName": "room-2",
            },
            True,
        )
    ]


def test_desktop_screen_share_stop_posts_stop(monkeypatch):
    calls = []

    monkeypatch.setattr(desktop_cli.platform, "system", lambda: "Darwin")

    def fake_request(method, path, *, json=None, launch=True):
        calls.append((method, path, json, launch))
        return {"ok": True, "state": "off"}

    monkeypatch.setattr(desktop_cli, "_desktop_control_request", fake_request)

    result = CliRunner().invoke(
        main, ["desktop", "screen-share", "stop", "--no-launch"]
    )

    assert result.exit_code == 0
    assert "Desktop screen share stopped (off)." in result.output
    assert calls == [("POST", "/livekit-companion/stop-screen-share", {}, False)]


def test_desktop_screen_share_rejects_linux(monkeypatch):
    monkeypatch.setattr(desktop_cli.platform, "system", lambda: "Linux")

    result = CliRunner().invoke(main, ["desktop", "screen-share", "status"])

    assert result.exit_code != 0
    assert "macOS Electron app" in result.output


def test_desktop_launch_prefers_current_app_name(monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setattr(desktop_cli.subprocess, "run", fake_run)

    desktop_cli._launch_desktop_app()

    assert calls == [["open", "-a", "Openbase"]]


def test_desktop_launch_falls_back_to_legacy_app_name(monkeypatch):
    calls = []

    def fake_run(args, **_kwargs):
        calls.append(args)
        return type("Result", (), {"returncode": 0 if len(calls) == 2 else 1})()

    monkeypatch.setattr(desktop_cli.subprocess, "run", fake_run)

    desktop_cli._launch_desktop_app()

    assert calls == [
        ["open", "-a", "Openbase"],
        ["open", "-a", "Openbase Coder"],
    ]


def test_desktop_launch_error_names_current_and_legacy_apps(monkeypatch):
    monkeypatch.setattr(
        desktop_cli.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 1})(),
    )

    with pytest.raises(click.ClickException) as exc_info:
        desktop_cli._launch_desktop_app()

    assert "Openbase.app or legacy Openbase Coder.app" in str(exc_info.value)
