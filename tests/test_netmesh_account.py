"""A VPN node must never carry a previous account's login into a new one."""

from __future__ import annotations

import importlib

import pytest
from click.testing import CliRunner

from openbase_coder_cli.services import cloud_registration
from openbase_coder_cli.services import netmesh_account as na
from openbase_coder_cli.services import netmesh_companion as nc
from openbase_coder_cli.services import tailscale_provider as tp

tailnet_cli = importlib.import_module("openbase_coder_cli.cli.tailnet")
auth_cli = importlib.import_module("openbase_coder_cli.cli.auth")


def _status(owner: str | None, ips=("100.64.0.136",), state="Running") -> dict:
    payload: dict = {"BackendState": state, "Self": {"TailscaleIPs": list(ips)}}
    if owner:
        payload["Self"]["UserID"] = 2
        payload["User"] = {
            "2": {"ID": 2, "LoginName": owner},
            "2147455555": {"ID": 2147455555, "LoginName": "tagged-devices"},
        }
    return payload


ENROLL_1806 = {
    "control_url": "https://net.example.test",
    "auth_key": "hskey-new",
    "tailnet_user": "ob-1806",
}


def test_node_owner_resolves_self_user_login() -> None:
    assert na.node_owner(_status("ob-1")) == "ob-1"
    assert na.node_owner(_status(None)) is None
    assert na.node_owner({"error": "down"}) is None
    assert na.node_owner(None) is None


def test_only_a_known_different_owner_is_a_mismatch() -> None:
    assert na.belongs_to_other_account(_status("ob-1"), ENROLL_1806)
    assert not na.belongs_to_other_account(_status("ob-1806"), ENROLL_1806)
    assert not na.belongs_to_other_account(_status(None), ENROLL_1806)
    assert not na.belongs_to_other_account(_status("ob-1"), None)
    assert not na.belongs_to_other_account(_status("ob-1"), {"auth_key": "k"})


def test_revoke_own_node_matches_by_address_only(monkeypatch) -> None:
    revoked: list[str] = []
    monkeypatch.setattr(
        cloud_registration,
        "list_netmesh_devices",
        lambda: [
            {"id": "7", "name": "same-name", "ip_addresses": ["100.64.0.9"]},
            {"id": "36", "name": "vm", "ip_addresses": ["100.64.0.136", "fd7a::1"]},
        ],
    )
    monkeypatch.setattr(
        cloud_registration,
        "revoke_netmesh_device",
        lambda node_id: revoked.append(node_id) or True,
    )
    assert na.revoke_own_node(_status("ob-1"))
    assert revoked == ["36"]


def test_revoke_own_node_never_deletes_without_an_address_match(monkeypatch) -> None:
    monkeypatch.setattr(
        cloud_registration,
        "list_netmesh_devices",
        lambda: [{"id": "7", "ip_addresses": ["100.64.0.9"]}],
    )
    monkeypatch.setattr(
        cloud_registration,
        "revoke_netmesh_device",
        lambda _id: pytest.fail("deleted a node that is not this machine"),
    )
    assert not na.revoke_own_node(_status("ob-1"))


def test_leave_network_logs_out_engine_then_deletes_node(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(na, "netmesh_status", lambda _p: _status("ob-1"))
    monkeypatch.setattr(
        na, "forget_node_login", lambda _p: order.append("forget") or None
    )
    monkeypatch.setattr(
        na, "revoke_own_node", lambda payload: order.append("revoke") or True
    )
    na.leave_network(tp.PROVIDER_NETMESH, echo=lambda _m: None)
    assert order == ["forget", "revoke"]


def test_leave_network_never_touches_the_users_own_tailscale(monkeypatch) -> None:
    monkeypatch.setattr(
        na, "forget_node_login", lambda _p: pytest.fail("logged out own tailnet")
    )
    na.leave_network(tp.PROVIDER_TAILSCALE, echo=lambda _m: None)


def test_sign_out_account_leaves_network_and_deregisters(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(tp, "provider", lambda: tp.PROVIDER_NETMESH)
    monkeypatch.setattr(
        na, "leave_network", lambda provider, echo: calls.append(f"leave:{provider}")
    )
    monkeypatch.setattr(
        cloud_registration,
        "deregister_device_with_cloud",
        lambda: (
            calls.append("deregister")
            or cloud_registration.CloudReportResult(ok=True, supported=True)
        ),
    )
    na.sign_out_account(echo=lambda _m: None)
    assert calls == ["leave:netmesh", "deregister"]


def test_companion_logout_failure_disconnects_instead(monkeypatch) -> None:
    calls: list[str] = []

    class Companion:
        def __init__(self, workspace_dir=None):
            pass

        def ensure_running(self, *, build_if_missing):
            return nc.CompanionStatus("Running", "enabled", "100.64.0.136", "vm", {})

        def replace_helper_if_needed(self):
            return nc.CompanionStatus("Running", "enabled", None, None, {})

        def logout(self):
            raise nc.NetmeshCompanionError("update the Openbase desktop app")

        def disconnect(self):
            calls.append("disconnect")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(tp, "netmesh_uses_stock_tailscale", lambda: False)
    monkeypatch.setattr(nc, "_workspace_dir_quiet", lambda: None)
    monkeypatch.setattr(nc, "NetmeshCompanion", Companion)
    error = na.forget_node_login(tp.PROVIDER_NETMESH)
    assert error and "desktop app" in error
    assert calls == ["disconnect", "close"]


class _ProvisionCompanion:
    def __init__(self, calls: list[str], *, running_after_logout: bool = False):
        self.calls = calls
        self.logged_out = False
        self.running_after_logout = running_after_logout

    def __call__(self, workspace_dir=None):
        return self

    def _state(self):
        running = not self.logged_out or self.running_after_logout
        return nc.CompanionStatus(
            "Running" if running else "NeedsLogin",
            "enabled",
            "100.64.0.136",
            "vm",
            {},
        )

    def ensure_running(self, build_if_missing=True):
        return self._state()

    def replace_helper_if_needed(self):
        return self._state()

    def status(self):
        return self._state()

    def connect(self, **kwargs):
        self.calls.append(f"connect:{kwargs['auth_key']}")
        return nc.CompanionStatus("Running", "enabled", "100.64.0.137", "vm", {})

    def close(self):
        pass


@pytest.fixture
def provisioning(monkeypatch):
    calls: list[str] = []
    companion = _ProvisionCompanion(calls)
    monkeypatch.setattr(tailnet_cli, "_dev_workspace_dir_or_none", lambda: None)
    monkeypatch.setattr(nc, "NetmeshCompanion", companion)
    monkeypatch.setattr(cloud_registration, "netmesh_enroll", lambda: ENROLL_1806)
    return calls, companion


def test_login_as_other_account_re_enrolls_instead_of_reusing_node(
    monkeypatch, provisioning, capsys
) -> None:
    calls, companion = provisioning
    monkeypatch.setattr(na, "netmesh_status", lambda _p: _status("ob-1"))

    def forget(provider):
        calls.append(f"forget:{provider}")
        companion.logged_out = True

    monkeypatch.setattr(na, "forget_node_login", forget)

    tailnet_cli._provision_netmesh_companion()

    assert calls == ["forget:netmesh", "connect:hskey-new"]
    assert "already connected" not in capsys.readouterr().out


def test_same_account_running_node_is_kept(monkeypatch, provisioning, capsys) -> None:
    calls, _companion = provisioning
    monkeypatch.setattr(na, "netmesh_status", lambda _p: _status("ob-1806"))
    monkeypatch.setattr(
        na, "forget_node_login", lambda _p: pytest.fail("signed out same account")
    )

    tailnet_cli._provision_netmesh_companion()

    assert calls == []
    assert "already connected" in capsys.readouterr().out


def test_unforgettable_foreign_login_is_never_reported_connected(
    monkeypatch, provisioning, capsys
) -> None:
    calls, _companion = provisioning
    monkeypatch.setattr(na, "netmesh_status", lambda _p: _status("ob-1"))
    monkeypatch.setattr(na, "forget_node_login", lambda _p: "helper too old")

    tailnet_cli._provision_netmesh_companion()

    out = capsys.readouterr().out
    assert calls == []
    assert "already connected" not in out
    assert "not connecting" in out


def test_enroll_json_signs_foreign_node_out_first(monkeypatch) -> None:
    order: list[str] = []
    monkeypatch.setattr(tp, "provider", lambda: tp.PROVIDER_NETMESH)
    monkeypatch.setattr(cloud_registration, "netmesh_enroll", lambda: ENROLL_1806)
    monkeypatch.setattr(na, "netmesh_status", lambda _p: _status("ob-1"))
    monkeypatch.setattr(
        na, "forget_node_login", lambda p: order.append(f"forget:{p}") or None
    )
    result = CliRunner().invoke(tailnet_cli.tailnet, ["enroll", "--json"])
    assert result.exit_code == 0, result.output
    assert order == ["forget:netmesh"]
    assert '"auth_key": "hskey-new"' in result.output


def test_enroll_json_refuses_when_foreign_login_persists(monkeypatch) -> None:
    monkeypatch.setattr(tp, "provider", lambda: tp.PROVIDER_NETMESH)
    monkeypatch.setattr(cloud_registration, "netmesh_enroll", lambda: ENROLL_1806)
    monkeypatch.setattr(na, "netmesh_status", lambda _p: _status("ob-1"))
    monkeypatch.setattr(na, "forget_node_login", lambda _p: "helper too old")
    result = CliRunner().invoke(tailnet_cli.tailnet, ["enroll", "--json"])
    assert result.exit_code != 0
    assert "hskey-new" not in result.output


def test_embedded_node_of_other_account_is_signed_out_and_rejoined(
    monkeypatch,
) -> None:
    from openbase_coder_cli.services import tunneld

    calls: list[str] = []
    statuses = iter([_status("ob-1")])
    monkeypatch.setattr(na, "netmesh_status", lambda _p: next(statuses))
    monkeypatch.setattr(
        na, "forget_node_login", lambda p: calls.append(f"forget:{p}") or None
    )
    monkeypatch.setattr(
        tunneld,
        "ensure_tunneld_running",
        lambda auth_key, managed_service: calls.append(f"ensure:{auth_key}"),
    )
    tailnet_cli._rejoin_embedded_node_for_account(ENROLL_1806, "hskey-new")
    assert calls == ["forget:netmesh-tsnet", "ensure:hskey-new"]


def test_logout_signs_out_account_before_removing_tokens(monkeypatch, tmp_path) -> None:
    auth_json = tmp_path / "auth.json"
    auth_json.write_text("{}")
    seen: list[bool] = []
    monkeypatch.setattr(auth_cli, "AUTH_JSON_PATH", auth_json)
    monkeypatch.setattr(auth_cli, "MACHINE_TOKEN_JSON_PATH", tmp_path / "m.json")
    monkeypatch.setattr(
        auth_cli, "sign_out_account", lambda echo: seen.append(auth_json.exists())
    )
    result = CliRunner().invoke(auth_cli.logout)
    assert result.exit_code == 0, result.output
    assert seen == [True]
    assert not auth_json.exists()


class _Manager:
    def __init__(self, sub: str | None, events: list[str]):
        self.sub = sub
        self.events = events
        self.has_refresh_token = sub is not None

    def __call__(self, _url):
        return self

    def get_owner_identity(self):
        return {"sub": self.sub} if self.sub else {}

    def store_tokens(self, **_kwargs):
        self.events.append("store")


def _jwt(sub: str) -> str:
    import base64
    import json

    body = base64.urlsafe_b64encode(json.dumps({"sub": sub}).encode()).decode()
    return f"h.{body.rstrip('=')}.s"


@pytest.mark.parametrize(
    ("previous", "new", "expected"),
    [
        ("1", "1806", ["sign-out", "store"]),
        ("1806", "1806", ["store"]),
        (None, "1806", ["store"]),
    ],
)
def test_login_signs_out_only_a_different_previous_account(
    monkeypatch, previous, new, expected
) -> None:
    events: list[str] = []
    monkeypatch.setattr(auth_cli, "TokenManager", _Manager(previous, events))
    monkeypatch.setattr(
        auth_cli, "sign_out_account", lambda echo: events.append("sign-out")
    )
    monkeypatch.setattr(
        auth_cli,
        "MachineTokenManager",
        lambda *_a: type("M", (), {"get_machine_token": lambda self, rotate: "t"})(),
    )
    monkeypatch.setattr(auth_cli, "reconcile_after_login", lambda: None)
    monkeypatch.setattr(
        auth_cli,
        "register_and_report",
        lambda: cloud_registration.CloudReportResult(ok=True, supported=True),
    )
    auth_cli._complete_login(
        web_backend_url="https://cloud.example.test",
        access_token=_jwt(new),
        refresh_token="r",
        expires_in=300,
    )
    assert events == expected


def test_deregister_posts_this_device_id(monkeypatch) -> None:
    sent: list[tuple[str, dict]] = []
    monkeypatch.setattr(cloud_registration, "_device_id", lambda: "desktop-abc")
    monkeypatch.setattr(
        cloud_registration,
        "_post_to_cloud",
        lambda path, payload, **_kw: (
            sent.append((path, payload))
            or cloud_registration.CloudReportResult(ok=True, supported=True)
        ),
    )
    assert cloud_registration.deregister_device_with_cloud().ok
    assert sent == [("/api/openbase/devices/deregister/", {"device_id": "desktop-abc"})]
