from __future__ import annotations

from types import SimpleNamespace

import pytest

from openbase_coder_cli.services import tunneld


def test_managed_tunneld_waits_for_control_api_without_spawning(monkeypatch) -> None:
    health = iter(
        [
            {"reachable": False, "error": "connection refused"},
            {"reachable": False, "error": "connection refused"},
            {
                "reachable": True,
                "backend_state": "Running",
                "forwards_up": True,
            },
        ]
    )
    monkeypatch.setattr(tunneld, "tunneld_health", lambda: next(health))
    monkeypatch.setattr(tunneld, "_managed_service_installed", lambda: True)
    monkeypatch.setattr(tunneld.time, "sleep", lambda _seconds: None)

    def unexpected_spawn(*_args, **_kwargs):
        pytest.fail("managed tunneld must not spawn a competing process")

    monkeypatch.setattr(tunneld.subprocess, "Popen", unexpected_spawn)

    tunneld.ensure_tunneld_running()


def test_managed_tunneld_timeout_fails_without_spawning(monkeypatch) -> None:
    monotonic = iter([0.0, 16.0])
    monkeypatch.setattr(
        tunneld,
        "tunneld_health",
        lambda: {"reachable": False, "error": "connection refused"},
    )
    monkeypatch.setattr(tunneld.time, "monotonic", lambda: next(monotonic))

    def unexpected_spawn(*_args, **_kwargs):
        pytest.fail("managed tunneld must not spawn a competing process")

    monkeypatch.setattr(tunneld.subprocess, "Popen", unexpected_spawn)

    with pytest.raises(RuntimeError, match="managed service did not reach Running"):
        tunneld.ensure_tunneld_running(managed_service=True)


def test_unmanaged_tunneld_keeps_standalone_start_fallback(monkeypatch) -> None:
    health = iter(
        [
            {"reachable": False, "error": "connection refused"},
            {
                "reachable": True,
                "backend_state": "Running",
                "forwards_up": True,
            },
        ]
    )
    calls = []
    monkeypatch.setattr(tunneld, "tunneld_health", lambda: next(health))
    monkeypatch.setattr(tunneld, "tunneld_binary", lambda: "/opt/openbase-tunneld")
    monkeypatch.setattr(
        tunneld.subprocess,
        "Popen",
        lambda command, **kwargs: (
            calls.append((command, kwargs)) or SimpleNamespace(pid=123)
        ),
    )

    tunneld.ensure_tunneld_running(managed_service=False)

    assert calls[0][0] == ["/opt/openbase-tunneld", "serve"]
    assert calls[0][1]["start_new_session"] is True


def test_managed_tunneld_enrolls_after_control_api_is_ready(monkeypatch) -> None:
    health = iter(
        [
            {"reachable": False, "error": "connection refused"},
            {"reachable": True, "backend_state": "NeedsLogin"},
            {
                "reachable": True,
                "backend_state": "Running",
                "forwards_up": True,
            },
        ]
    )
    submitted = []
    monkeypatch.setattr(tunneld, "tunneld_health", lambda: next(health))
    monkeypatch.setattr(tunneld.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        tunneld,
        "tunneld_login",
        lambda auth_key: submitted.append(auth_key) or True,
    )

    tunneld.ensure_tunneld_running(
        auth_key="single-use-test-key",
        managed_service=True,
    )

    assert submitted == ["single-use-test-key"]
