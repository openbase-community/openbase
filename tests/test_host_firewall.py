"""macOS application firewall detection for the login relay."""

import subprocess

import pytest

from openbase_coder_cli import host_firewall


@pytest.fixture
def macos(monkeypatch, tmp_path):
    tool = tmp_path / "socketfilterfw"
    tool.write_text("")
    monkeypatch.setattr(host_firewall.sys, "platform", "darwin")
    monkeypatch.setattr(host_firewall, "SOCKETFILTERFW", str(tool))
    answers = {}

    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, answers.get(argv[1], ""), "")

    monkeypatch.setattr(host_firewall.subprocess, "run", run)
    return answers


def _set(
    answers, *, state="enabled. (State = 1)", block_all="disabled", app="permitted"
):
    answers["--getglobalstate"] = f"Firewall is {state}"
    answers["--getblockall"] = f"Firewall has block all state set to {block_all}."
    answers["--getappblocked"] = f"Incoming connection to /x is {app}."


def test_blocked_program_is_reported(macos):
    _set(macos, app="blocked")
    assert host_firewall.blocked_interpreter("/x") == "/x"


def test_allowed_program_firewall_off_or_block_all(macos):
    _set(macos)
    assert host_firewall.blocked_interpreter("/x") is None
    _set(macos, state="disabled. (State = 0)", app="blocked")
    assert host_firewall.blocked_interpreter("/x") is None
    _set(macos, block_all="enabled")
    assert host_firewall.blocked_interpreter("/x") == "/x"


def test_other_platforms_and_unreadable_setting(monkeypatch, macos):
    _set(macos, app="blocked")

    def broken(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 5)

    monkeypatch.setattr(host_firewall.subprocess, "run", broken)
    assert host_firewall.blocked_interpreter("/x") is None
    monkeypatch.setattr(host_firewall.sys, "platform", "linux")
    assert host_firewall.blocked_interpreter("/x") is None


def test_hint_points_the_user_at_settings_never_a_command():
    hint = host_firewall.unblock_hint("/opt/py/bin/python3.12")
    assert "System Settings > Network > Firewall > Options" in hint
    assert "python3.12" in hint and "needs your password" in hint
    assert "sudo" not in hint and "--unblockapp" not in hint
