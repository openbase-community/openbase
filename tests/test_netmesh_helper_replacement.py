"""A helper handoff may require a fresh companion request after unregister."""

from unittest.mock import Mock

import pytest

from openbase_coder_cli.services import netmesh_companion as nc


@pytest.fixture(autouse=True)
def macos_platform(monkeypatch):
    monkeypatch.setattr(nc.sys, "platform", "darwin")


def test_pending_replacement_continues_and_verifies(monkeypatch):
    companion = nc.NetmeshCompanion(None)
    request = Mock(
        side_effect=[
            {"ok": True, "helper": "notRegistered", "helperReplacementPending": True},
            {"ok": True, "helper": "enabled"},
            {
                "ok": True,
                "helper": "enabled",
                "backendState": "Running",
                "helperReplaced": True,
            },
        ]
    )
    monkeypatch.setattr(companion, "_request", request)
    monkeypatch.setattr(nc.time, "sleep", Mock())
    result = companion.replace_helper_if_needed()
    assert result.running and result.helper_enabled
    assert [call.args for call in request.call_args_list] == [
        ("POST", "/replace-helper"),
        ("POST", "/register"),
        ("POST", "/replace-helper"),
    ]


def test_pending_replacement_is_bounded(monkeypatch):
    companion = nc.NetmeshCompanion(None)
    request = Mock(
        return_value={
            "ok": True,
            "helper": "notRegistered",
            "helperReplacementPending": True,
        }
    )
    monkeypatch.setattr(companion, "_request", request)
    monkeypatch.setattr(nc.time, "sleep", Mock())
    with pytest.raises(nc.NetmeshCompanionError, match="did not complete"):
        companion.replace_helper_if_needed()
    assert request.call_count == 11


@pytest.mark.parametrize("helper", ["requiresApproval", "enabled", "notFound"])
def test_pending_cannot_override_other_helper_states(monkeypatch, helper):
    companion = nc.NetmeshCompanion(None)
    request = Mock(
        return_value={
            "ok": True,
            "helper": helper,
            "helperReplacementPending": True,
        }
    )
    monkeypatch.setattr(companion, "_request", request)
    with pytest.raises(nc.NetmeshCompanionError, match="cannot continue"):
        companion.replace_helper_if_needed()
    assert request.call_count == 1


def test_replacement_failure_is_not_retried(monkeypatch):
    companion = nc.NetmeshCompanion(None)
    request = Mock(return_value={"ok": False, "error": "registration failed"})
    monkeypatch.setattr(companion, "_request", request)
    with pytest.raises(nc.NetmeshCompanionError, match="registration failed"):
        companion.replace_helper_if_needed()
    assert request.call_count == 1


def test_successful_registration_still_requires_version_verification(monkeypatch):
    companion = nc.NetmeshCompanion(None)
    request = Mock(
        side_effect=[
            {"ok": True, "helper": "notRegistered", "helperReplacementPending": True},
            {"ok": True, "helper": "enabled"},
            {"ok": False, "error": "wrong helper version"},
        ]
    )
    monkeypatch.setattr(companion, "_request", request)
    monkeypatch.setattr(nc.time, "sleep", Mock())
    with pytest.raises(nc.NetmeshCompanionError, match="wrong helper version"):
        companion.replace_helper_if_needed()
    assert request.call_count == 3


def test_approval_during_continuation_stops_registration(monkeypatch):
    companion = nc.NetmeshCompanion(None)
    request = Mock(
        side_effect=[
            {"ok": True, "helper": "notRegistered", "helperReplacementPending": True},
            {"ok": True, "helper": "requiresApproval"},
        ]
    )
    monkeypatch.setattr(companion, "_request", request)
    monkeypatch.setattr(nc.time, "sleep", Mock())
    with pytest.raises(nc.NetmeshCompanionError, match="requiresApproval"):
        companion.replace_helper_if_needed()
    assert request.call_count == 2
