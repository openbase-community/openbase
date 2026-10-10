from __future__ import annotations

from types import SimpleNamespace

import pytest

from openbase_coder_cli.services import tunneld


def test_tunneld_node_enrolled_requires_complete_profile_markers(
    tmp_path, monkeypatch
) -> None:
    state_dir = tmp_path / "tsnet"
    state_dir.mkdir()
    monkeypatch.setattr(tunneld, "_state_dir", lambda: state_dir)
    state_file = state_dir / "tailscaled.state"

    state_file.write_text('{"_machinekey":"opaque"}', encoding="utf-8")
    assert tunneld.tunneld_node_enrolled() is False

    state_file.write_text(
        '{"_current-profile":"opaque","_profiles":"opaque","profile-abcd":"opaque"}',
        encoding="utf-8",
    )
    assert tunneld.tunneld_node_enrolled() is True


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


def test_add_forward_sends_service_forward_fields(monkeypatch) -> None:
    import httpx

    posted = {}

    def fake_post(url, *, json, headers, timeout):
        posted.update(url=url, json=json)
        return httpx.Response(201, json={"port": 443, "local_port": 59443})

    monkeypatch.setattr(tunneld.httpx, "post", fake_post)
    monkeypatch.setattr(tunneld, "_control_headers", lambda: {})

    tunneld.tunneld_add_forward(443, local_port=59443, persistent=True)
    assert posted["url"].endswith("/forwards")
    assert posted["json"] == {
        "port": 443,
        "one_shot": False,
        "local_port": 59443,
        "persistent": True,
    }

    tunneld.tunneld_add_forward(3000, ttl_seconds=60)
    assert posted["json"] == {"port": 3000, "one_shot": False, "ttl_seconds": 60}


def test_resolve_returns_the_daemon_resolvers_addresses(monkeypatch) -> None:
    import httpx

    answers = {"status": 200, "json": {"addresses": ["100.64.0.10"]}}

    def fake_get(url, *, params, headers, timeout):
        assert url.endswith("/resolve")
        assert params == {"name": "crm.abcdefghijkl.vpn.obs.so"}
        return httpx.Response(answers["status"], json=answers["json"])

    monkeypatch.setattr(tunneld.httpx, "get", fake_get)
    monkeypatch.setattr(tunneld, "_control_headers", lambda: {})

    assert tunneld.tunneld_resolve("crm.abcdefghijkl.vpn.obs.so") == ["100.64.0.10"]

    answers.update(status=502, json={"error": "lookup failed"})
    with pytest.raises(RuntimeError, match="lookup failed"):
        tunneld.tunneld_resolve("crm.abcdefghijkl.vpn.obs.so")
