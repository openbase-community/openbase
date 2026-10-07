"""Openbase Sync pairing: candidates, hub, join (via the hub's offer), leave, roots.

Peers and the hub's offer are mocked at the HTTP layer; the real config file
lives in the per-test temporary home.
"""

from __future__ import annotations

# ruff: noqa: E402, I001

import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django

django.setup()

import httpx
import pytest
from click.testing import CliRunner
from rest_framework.test import APIRequestFactory, force_authenticate

from openbase_coder_cli import skills_sync, sync_daemon, sync_pairing
from openbase_coder_cli.cli.sync_daemon import sync_daemon_cli
from openbase_coder_cli.openbase_coder_cli_app import sync_daemon_api, sync_pairing_api
from openbase_coder_cli.services import fleet_aggregation as fleet
from openbase_coder_cli.services import network

SECRET = "0123456789abcdef0123456789abcdef"
MINI = fleet.FleetPeer(
    key="mini.net.example",
    name="mini",
    base_url="http://mini.net.example:18080",
    ip="100.64.0.2",
    os="macOS",
)
DESK = fleet.FleetPeer(
    key="desk.net.example",
    name="desk",
    base_url="http://desk.net.example:18080",
    ip="100.64.0.3",
    os="linux",
)
PHONE = fleet.FleetPeer(
    key="phone.net.example",
    name="phone",
    base_url="http://phone.net.example:18080",
    ip="100.64.0.9",
    os="iOS",
)


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sync_daemon, "OPENBASE_BASE_DIR", home / ".openbase")
    monkeypatch.setattr(sync_pairing, "OPENBASE_BASE_DIR", home / ".openbase")
    monkeypatch.setattr(skills_sync, "linked_source_paths", lambda: [])
    return home


@pytest.fixture
def env(home, monkeypatch):
    """A signed-in computer with the daemon binary, on Openbase VPN."""
    state = SimpleNamespace(
        started=0,
        stopped=0,
        restarted=0,
        token="owner-jwt",
        peers=[MINI, DESK, PHONE],
        requests=[],
        self_hosts={"laptop.net.example", "100.64.0.1"},
    )
    monkeypatch.setattr(sync_pairing, "daemon_binary_available", lambda: True)
    monkeypatch.setattr(network, "tailscale_ip", lambda family="4": "100.64.0.1")

    def start():
        state.started += 1

    def stop():
        state.stopped += 1

    def restart():
        state.restarted += 1
        return True

    monkeypatch.setattr(sync_pairing, "_start_service", start)
    monkeypatch.setattr(sync_pairing, "_stop_service", stop)
    monkeypatch.setattr(sync_daemon, "restart_service_if_installed", restart)
    monkeypatch.setattr(fleet, "owner_access_token", lambda: state.token)
    monkeypatch.setattr(
        fleet, "fleet_peers", lambda include_failed=False: list(state.peers)
    )
    monkeypatch.setattr(sync_pairing, "_self_hosts", lambda: state.self_hosts)
    monkeypatch.setattr(
        sync_daemon, "default_device_id", lambda: "desktop-this-computer"
    )
    return state


def _response(status: int, payload=None) -> httpx.Response:
    return httpx.Response(
        status, json=payload, request=httpx.Request("GET", "http://peer")
    )


def _config() -> dict:
    return tomllib.loads(sync_daemon.SYNC_DAEMON_CONFIG_PATH.read_text())


def _write(role: str, roots: list[dict], **extra) -> None:
    sync_daemon.write_config(
        sync_daemon.SyncDaemonConfig(
            device_id="desktop-this-computer",
            sync_group="default",
            role=role,
            pair_secret=SECRET,
            roots=roots,
            listen_hot="100.64.0.1:22100",
            listen_bulk="100.64.0.1:22101",
            peer_hot="mini.net.example:22100",
            peer_bulk="mini.net.example:22101",
            anchor=extra.get("anchor", "edge"),
        )
    )


def _request(method: str, path: str, data: dict | None = None):
    factory = APIRequestFactory()
    fn = {"GET": factory.get, "POST": factory.post, "DELETE": factory.delete}[method]
    request = fn(path, data=data or {}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    return request


PROJECTS = {"id": "projects", "path": "~/Projects"}
THREADS = {"id": "openbase-thread-sync", "path": "~/.openbase/thread-sync"}


# --- candidates -------------------------------------------------------------


def test_candidates_report_roles_and_skip_phones(env, monkeypatch):
    def fake_get(url, headers, timeout):
        assert headers == {"Authorization": "Bearer owner-jwt"}
        if url.startswith(MINI.base_url):
            return _response(200, {"configured": True, "role": "hub", "roots": []})
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", fake_get)

    payload = sync_pairing.candidates()

    assert payload["signed_in"] is True
    assert payload["role"] == "none"
    by_name = {entry["name"]: entry for entry in payload["candidates"]}
    assert set(by_name) == {"mini", "desk"}
    assert by_name["mini"]["role"] == "hub" and by_name["mini"]["reachable"]
    assert by_name["desk"]["role"] == "unknown"
    assert not by_name["desk"]["reachable"]
    assert by_name["desk"]["error"]


def test_candidates_name_an_edges_hub_and_flag_other_accounts(env, monkeypatch):
    def fake_get(url, headers, timeout):
        if url.startswith(MINI.base_url):
            return _response(
                200,
                {"configured": True, "role": "edge", "peer_hot": "100.64.0.1:22100"},
            )
        return _response(403, {"detail": "Token identity is not authorized"})

    monkeypatch.setattr(httpx, "get", fake_get)

    by_name = {e["name"]: e for e in sync_pairing.candidates()["candidates"]}

    assert by_name["mini"]["role"] == "edge"
    assert by_name["mini"]["hub_name"] == "this computer"
    assert by_name["desk"]["reachable"] is True
    assert by_name["desk"]["role"] == "unknown"
    assert "different Openbase account" in by_name["desk"]["error"]


def test_candidates_without_sign_in_do_not_call_peers(env, monkeypatch):
    env.token = None
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: pytest.fail("must not call peers")
    )

    payload = sync_pairing.candidates()

    assert payload["signed_in"] is False
    assert all(entry["role"] == "unknown" for entry in payload["candidates"])


# --- hub ----------------------------------------------------------------------


def test_become_hub_writes_config_and_starts_service(env, home):
    result = sync_pairing.become_hub()

    config = _config()
    assert config["role"] == "hub"
    assert config["listen_hot"] == "100.64.0.1:22100"
    assert config["listen_bulk"] == "100.64.0.1:22101"
    assert len(config["pair_secret"]) == 32
    assert config["placement"]["anchor"] == "edge"
    paths = [root["path"] for root in config["roots"]]
    assert paths == ["~/Projects", "~/.openbase/thread-sync", "~/.agents/skills"]
    assert (home / "Projects").is_dir()
    assert env.started == 1
    assert result["role"] == "hub"
    assert "pair_secret" not in str(result)
    assert SECRET not in str(result) and config["pair_secret"] not in str(result)


def test_become_hub_refuses_when_already_syncing(env):
    _write("edge", [PROJECTS])
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.become_hub()
    assert excinfo.value.code == "already_configured"
    assert env.started == 0


def test_become_hub_needs_the_daemon_binary(env, monkeypatch):
    monkeypatch.setattr(sync_pairing, "daemon_binary_available", lambda: False)
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.become_hub()
    assert excinfo.value.code == "binaries_missing"
    assert "Update Openbase" in str(excinfo.value)
    assert not sync_daemon.is_configured()


def test_become_hub_needs_openbase_vpn(env, monkeypatch):
    monkeypatch.setattr(network, "tailscale_ip", lambda family="4": None)
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.become_hub()
    assert excinfo.value.code == "no_vpn"
    assert not sync_daemon.is_configured()


# --- offer --------------------------------------------------------------------


def test_offer_requires_a_hub(env):
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.offer()
    assert excinfo.value.code == "hub_not_configured"

    _write("edge", [PROJECTS])
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.offer()
    assert excinfo.value.code == "hub_not_configured"


def test_offer_view_hands_over_secret_ports_and_roots(env):
    _write("hub", [PROJECTS, THREADS])

    response = sync_pairing_api.sync_pairing_offer(
        _request("POST", "/api/sync/daemon/pairing/offer/")
    )

    assert response.status_code == 200
    assert response.data == {
        "pair_secret": SECRET,
        "hot_port": 22100,
        "bulk_port": 22101,
        "sync_group": "default",
        "anchor": "edge",
        "roots": [PROJECTS, THREADS],
    }


def test_offer_view_refuses_on_a_non_hub(env):
    response = sync_pairing_api.sync_pairing_offer(
        _request("POST", "/api/sync/daemon/pairing/offer/")
    )
    assert response.status_code == 409
    assert response.data["code"] == "hub_not_configured"


# --- join -----------------------------------------------------------------------


def _offer_payload(**overrides) -> dict:
    payload = {
        "pair_secret": SECRET,
        "hot_port": 22100,
        "bulk_port": 22101,
        "sync_group": "default",
        "anchor": "edge",
        "roots": [PROJECTS, THREADS],
    }
    payload.update(overrides)
    return payload


def _fake_post(env, response):
    def post(url, headers, json, timeout):
        env.requests.append((url, headers))
        if isinstance(response, Exception):
            raise response
        return response

    return post


def test_join_writes_edge_config_from_the_hubs_offer(env, home, monkeypatch):
    monkeypatch.setattr(
        httpx, "post", _fake_post(env, _response(200, _offer_payload()))
    )

    result = sync_pairing.join_hub("mini")

    assert env.requests == [
        (
            "http://mini.net.example:18080/api/sync/daemon/pairing/offer/",
            {"Authorization": "Bearer owner-jwt"},
        )
    ]
    config = _config()
    assert config["role"] == "edge"
    assert config["pair_secret"] == SECRET
    assert config["peer_hot"] == "mini.net.example:22100"
    assert config["peer_bulk"] == "mini.net.example:22101"
    assert config["placement"]["anchor"] == "edge"
    assert config["roots"] == [PROJECTS, THREADS]
    assert (home / ".openbase" / "thread-sync").is_dir()
    assert env.started == 1
    assert result == {
        "role": "edge",
        "hub_name": "mini",
        "hub_host": "mini.net.example",
        "roots": [PROJECTS, THREADS],
    }


def test_join_can_pick_a_subset_of_the_hubs_folders(env, monkeypatch):
    monkeypatch.setattr(
        httpx, "post", _fake_post(env, _response(200, _offer_payload()))
    )

    sync_pairing.join_hub("100.64.0.2", ["~/Projects"])

    assert _config()["roots"] == [PROJECTS]


def test_join_refuses_folders_the_hub_does_not_sync(env, monkeypatch):
    monkeypatch.setattr(
        httpx, "post", _fake_post(env, _response(200, _offer_payload()))
    )

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.join_hub("mini", ["~/Documents"])

    assert excinfo.value.code == "root_not_on_hub"
    assert not sync_daemon.is_configured()


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.ConnectError("refused"), "hub_unreachable"),
        (_response(403, {"detail": "not authorized"}), "other_account"),
        (_response(401, {"detail": "no owner"}), "other_account"),
        (_response(409, {"code": "hub_not_configured"}), "hub_not_configured"),
        (_response(404, {"detail": "not found"}), "hub_outdated"),
        (_response(500, {"detail": "boom"}), "hub_error"),
        (_response(200, {"roots": []}), "hub_error"),
    ],
)
def test_join_errors_are_clear_and_change_nothing(env, monkeypatch, response, code):
    monkeypatch.setattr(httpx, "post", _fake_post(env, response))

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.join_hub("mini")

    assert excinfo.value.code == code
    assert "mini" in str(excinfo.value)
    assert not sync_daemon.is_configured()
    assert env.started == 0


def test_join_only_contacts_the_users_own_computers(env, monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda *a, **k: pytest.fail("no request"))

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.join_hub("evil.example.com")

    assert excinfo.value.code == "hub_not_found"


def test_join_needs_sign_in(env, monkeypatch):
    env.token = None
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.join_hub("mini")
    assert excinfo.value.code == "not_signed_in"


def test_join_view_maps_errors_to_json(env, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_post(env, httpx.ConnectError("x")))

    response = sync_pairing_api.sync_pairing_join(
        _request("POST", "/api/sync/daemon/pairing/join/", {"hub": "mini"})
    )

    assert response.status_code == 502
    assert response.data["code"] == "hub_unreachable"
    assert "Could not reach mini" in response.data["error"]

    missing = sync_pairing_api.sync_pairing_join(
        _request("POST", "/api/sync/daemon/pairing/join/", {})
    )
    assert missing.status_code == 400


def test_join_view_success_never_echoes_the_secret(env, monkeypatch):
    monkeypatch.setattr(
        httpx, "post", _fake_post(env, _response(200, _offer_payload()))
    )
    monkeypatch.setattr(sync_pairing, "refresh_cloud_registration", lambda **k: None)

    response = sync_pairing_api.sync_pairing_join(
        _request("POST", "/api/sync/daemon/pairing/join/", {"hub": "mini"})
    )

    assert response.status_code == 200
    assert SECRET not in str(response.data)


# --- GET responses never carry the secret --------------------------------------


def test_get_endpoints_never_return_the_pair_secret(env, monkeypatch):
    _write("edge", [PROJECTS])
    monkeypatch.setattr(sync_daemon, "reachable", lambda: False)
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _response(200, {"configured": False})
    )

    settings = sync_daemon_api.sync_daemon_settings(
        _request("GET", "/api/sync/daemon/settings/")
    )
    roots = sync_pairing_api.sync_daemon_roots(
        _request("GET", "/api/sync/daemon/roots/")
    )
    candidates = sync_pairing_api.sync_pairing_candidates(
        _request("GET", "/api/sync/daemon/pairing/candidates/")
    )

    for response in (settings, roots, candidates):
        assert response.status_code == 200
        assert SECRET not in str(response.data)
        assert "pair_secret" not in str(response.data)
    assert settings.data["hub_name"] == "mini"
    assert settings.data["hub_host"] == "mini.net.example"
    assert settings.data["hub_is_self"] is False


def test_settings_marks_a_hub_as_self(env, monkeypatch):
    _write("hub", [PROJECTS])
    monkeypatch.setattr(sync_daemon, "reachable", lambda: True)

    settings = sync_daemon_api.sync_daemon_settings(
        _request("GET", "/api/sync/daemon/settings/")
    )

    assert settings.data["hub_is_self"] is True
    assert settings.data["role"] == "hub"


def test_unconfigured_settings_stay_neutral(env, monkeypatch):
    settings = sync_daemon_api.sync_daemon_settings(
        _request("GET", "/api/sync/daemon/settings/")
    )
    assert settings.data["configured"] is False
    assert "hub_name" not in settings.data
    assert env.started == 0


# --- leave ----------------------------------------------------------------------


def test_leave_stops_service_and_moves_config_to_trash(env, home):
    _write("edge", [PROJECTS])

    result = sync_pairing.leave()

    assert env.stopped == 1
    assert not sync_daemon.is_configured()
    moved = Path(result["config_moved_to"])
    assert moved.parent == home / ".openbase" / "trash"
    assert tomllib.loads(moved.read_text())["role"] == "edge"
    assert result["left"] is True


def test_leave_when_not_syncing_is_a_no_op(env):
    assert sync_pairing.leave() == {"left": False, "config_moved_to": None}
    assert env.stopped == 0


# --- roots ----------------------------------------------------------------------


def _fake_request(env, response):
    def request(method, url, headers, json, timeout):
        env.requests.append((method, url, json))
        if isinstance(response, Exception):
            raise response
        return response

    return request


def test_edge_adds_a_root_on_the_hub_first(env, home, monkeypatch):
    _write("edge", [PROJECTS])
    (home / "Documents").mkdir()
    monkeypatch.setattr(httpx, "request", _fake_request(env, _response(200, {})))

    result = sync_pairing.add_root("~/Documents")

    assert env.requests == [
        (
            "POST",
            "http://mini.net.example:18080/api/sync/daemon/roots/",
            {"path": "~/Documents", "local_only": True},
        )
    ]
    assert [root["path"] for root in sync_daemon.configured_roots()] == [
        "~/Projects",
        "~/Documents",
    ]
    assert result["peers"] == [{"name": "mini", "ok": True, "error": None}]
    assert env.restarted == 1


def test_edge_root_change_is_not_made_when_the_hub_is_unreachable(
    env, home, monkeypatch
):
    _write("edge", [PROJECTS])
    (home / "Documents").mkdir()
    monkeypatch.setattr(
        httpx, "request", _fake_request(env, httpx.ConnectError("down"))
    )

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.add_root("~/Documents")

    assert excinfo.value.code == "peer_unreachable"
    assert sync_daemon.configured_roots() == [PROJECTS]
    assert env.restarted == 0


def test_hub_adds_locally_then_updates_its_edges(env, home, monkeypatch):
    _write("hub", [PROJECTS])
    (home / "Documents").mkdir()

    def fake_get(url, headers, timeout):
        if url.startswith(MINI.base_url):
            return _response(
                200,
                {"configured": True, "role": "edge", "peer_hot": "100.64.0.1:22100"},
            )
        return _response(200, {"configured": False})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "request", _fake_request(env, _response(200, {})))

    result = sync_pairing.add_root("~/Documents")

    assert [r[1] for r in env.requests] == [
        "http://mini.net.example:18080/api/sync/daemon/roots/"
    ]
    assert result["peers"] == [{"name": "mini", "ok": True, "error": None}]
    assert len(sync_daemon.configured_roots()) == 2


def test_local_only_add_creates_the_folder_and_is_idempotent(env, home):
    _write("hub", [PROJECTS])

    sync_pairing.add_root("~/Documents", local_only=True)
    again = sync_pairing.add_root("~/Documents", local_only=True)

    assert (home / "Documents").is_dir()
    assert again["root"] is None
    assert len(sync_daemon.configured_roots()) == 2


def test_add_root_requires_an_existing_folder_and_no_overlap(env, home):
    _write("hub", [PROJECTS])

    with pytest.raises(sync_pairing.PairingError) as missing:
        sync_pairing.add_root("~/Nope")
    (home / "Projects" / "app").mkdir(parents=True)
    with pytest.raises(sync_pairing.PairingError) as overlap:
        sync_pairing.add_root("~/Projects/app")

    assert missing.value.code == "folder_missing"
    assert overlap.value.code == "root_overlap"


def test_remove_root_keeps_at_least_one_folder(env, monkeypatch):
    _write("hub", [PROJECTS, THREADS])
    monkeypatch.setattr(httpx, "get", lambda *a, **k: httpx.ConnectError("x"))
    monkeypatch.setattr(sync_pairing, "_edge_peers", lambda: [])

    result = sync_pairing.remove_root("~/.openbase/thread-sync")
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.remove_root("~/Projects")

    assert result["roots"] == [PROJECTS]
    assert excinfo.value.code == "last_root"


def test_roots_view_add_and_remove(env, home, monkeypatch):
    _write("hub", [PROJECTS])
    (home / "Documents").mkdir()
    monkeypatch.setattr(sync_pairing, "_edge_peers", lambda: [])

    added = sync_pairing_api.sync_daemon_roots(
        _request("POST", "/api/sync/daemon/roots/", {"path": "~/Documents"})
    )
    removed = sync_pairing_api.sync_daemon_roots(
        _request("DELETE", "/api/sync/daemon/roots/", {"path": "~/Documents"})
    )
    unknown = sync_pairing_api.sync_daemon_roots(
        _request("DELETE", "/api/sync/daemon/roots/", {"path": "~/Elsewhere"})
    )

    assert added.status_code == 200 and added.data["root"]["path"] == "~/Documents"
    assert removed.status_code == 200
    assert removed.data["roots"] == [PROJECTS]
    assert unknown.status_code == 404


def test_roots_view_refuses_when_not_configured(env):
    response = sync_pairing_api.sync_daemon_roots(
        _request("POST", "/api/sync/daemon/roots/", {"path": "~/Projects"})
    )
    assert response.status_code == 409
    assert response.data["code"] == "not_configured"


# --- CLI --------------------------------------------------------------------------


def test_cli_pair_hub_and_leave(env, monkeypatch):
    monkeypatch.setattr(
        sync_pairing, "refresh_cloud_registration", lambda **kwargs: None
    )
    runner = CliRunner()

    hub = runner.invoke(sync_daemon_cli, ["pair", "hub", "--root", "~/Projects"])
    leave = runner.invoke(sync_daemon_cli, ["pair", "leave", "--yes"])

    assert hub.exit_code == 0, hub.output
    assert "now the hub" in hub.output
    assert "~/Projects" in hub.output
    assert leave.exit_code == 0, leave.output
    assert "Stopped syncing" in leave.output
    assert not sync_daemon.is_configured()


def test_cli_pair_join_reports_errors(env, monkeypatch):
    monkeypatch.setattr(httpx, "post", _fake_post(env, _response(409, {})))

    result = CliRunner().invoke(sync_daemon_cli, ["pair", "join", "mini"])

    assert result.exit_code == 1
    assert "not set up as the always-on computer" in result.output


def test_cli_pair_candidates_lists_roles(env, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda url, headers, timeout: _response(
            200, {"configured": True, "role": "hub"}
        ),
    )

    result = CliRunner().invoke(sync_daemon_cli, ["pair", "candidates"])

    assert result.exit_code == 0, result.output
    assert "mini" in result.output and "hub" in result.output
    assert "phone" not in result.output
