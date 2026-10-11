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
REAL_START_SERVICE = sync_pairing._start_service
REAL_STOP_SERVICE = sync_pairing._stop_service
REAL_ROOT_ESTIMATES = sync_pairing._root_estimates
REAL_ENGINE_FEATURES = sync_pairing.engine_features
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
    monkeypatch.setattr(sync_pairing, "is_cloud_workspace", lambda: False)
    monkeypatch.setattr(sync_pairing, "engine_features", lambda: {"project-only"})
    # no daemon in tests: folder sizes come from a stub (``_root_estimates``)
    monkeypatch.setattr(sync_pairing, "_root_estimates", lambda: {})
    monkeypatch.setattr(network, "tailscale_ip", lambda family="4": "100.64.0.1")

    def start():
        state.started += 1
        return False

    def stop():
        state.stopped += 1
        return False

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
            thin=extra.get("thin"),
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


def test_become_hub_needs_sign_in(env):
    env.token = None
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.become_hub()
    assert excinfo.value.code == "not_signed_in"
    assert not sync_daemon.is_configured()
    assert env.started == 0


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
        "roots": [
            {**PROJECTS, "files": None, "bytes": None},
            {**THREADS, "files": None, "bytes": None},
        ],
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
        "project_only": False,
        "warnings": [],
        "restart_required": False,
    }
    assert "thin" not in config["placement"]
    # the floor is derived from the disk size by the daemon, not fixed here
    assert "low_water_mb" not in config["placement"]


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
    assert sync_pairing.leave() == {
        "left": False,
        "config_moved_to": None,
        "restart_required": False,
    }
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


def test_cli_pair_reports_when_restart_is_required(env, monkeypatch):
    monkeypatch.setattr(
        sync_pairing, "refresh_cloud_registration", lambda **kwargs: None
    )
    monkeypatch.setattr(sync_pairing, "_start_service", lambda: True)
    monkeypatch.setattr(sync_pairing, "_stop_service", lambda: True)
    runner = CliRunner()

    hub = runner.invoke(sync_daemon_cli, ["pair", "hub", "--root", "~/Projects"])
    leave = runner.invoke(sync_daemon_cli, ["pair", "leave", "--yes"])

    assert hub.exit_code == 0, hub.output
    assert "Restart Openbase to finish." in hub.output
    assert leave.exit_code == 0, leave.output
    assert "Restart Openbase to finish." in leave.output


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


@pytest.mark.parametrize("path", ["~", "/", "~/.openbase", "STATE", "STATE/x"])
def test_roots_that_must_never_sync_are_refused(env, path):
    _write("hub", [PROJECTS])
    path = path.replace("STATE", str(sync_daemon.SYNC_DAEMON_CONFIG_PATH.parent))

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.add_root(path, local_only=True)

    assert excinfo.value.code == "root_not_allowed"
    assert sync_daemon.configured_roots() == [PROJECTS]


def test_hub_refuses_a_root_that_must_never_sync(env):
    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.become_hub(["~"])
    assert excinfo.value.code == "root_not_allowed"
    assert not sync_daemon.is_configured()


def test_external_supervisor_writes_the_wrapper_and_asks_for_a_restart(
    env, home, monkeypatch
):
    """The Docker image starts the sync daemon only at container start."""
    from openbase_coder_cli.services import installation, launchd

    monkeypatch.setattr(
        installation.InstallationConfig, "load", classmethod(lambda cls: object())
    )
    monkeypatch.setattr(sync_pairing, "_start_service", REAL_START_SERVICE)
    monkeypatch.setattr(sync_pairing, "_stop_service", REAL_STOP_SERVICE)
    monkeypatch.setattr(sync_pairing, "_externally_supervised", lambda: True)
    regenerated = []
    monkeypatch.setattr(
        launchd, "regenerate_service", lambda config, svc: regenerated.append(svc)
    )
    monkeypatch.setattr(
        launchd,
        "install_service",
        lambda *a: pytest.fail("no launchd/systemd under an external supervisor"),
    )
    wrapper = home / "sync-daemon.sh"
    wrapper.write_text("#!/bin/sh\n")
    monkeypatch.setattr(launchd, "_wrapper_path", lambda svc: wrapper)

    hub = sync_pairing.become_hub(["~/Projects"])
    left = sync_pairing.leave()

    assert hub["restart_required"] is True
    assert [svc.name for svc in regenerated] == ["sync-daemon"]
    assert left["restart_required"] is True
    assert not wrapper.exists()


# --- project-only computers (a small cloud workspace) ---------------------------


ALPHA = {"id": "projects-alpha", "path": "~/Projects/alpha"}
BETA = {"id": "projects-beta", "path": "~/Projects/beta"}


def _hub_routes(env, monkeypatch, *, folders=None, offer=None):
    """Fake hub: GET pairing/folders/ (sizes, no secret) and POST offer."""
    folders = (
        folders
        if folders is not None
        else _response(
            200,
            {
                "roots": [
                    {**ALPHA, "files": 1200, "bytes": 3_000_000_000},
                    {**BETA, "files": 80, "bytes": 40_000_000},
                ],
                "disk": {"free_bytes": 1, "total_bytes": 2},
            },
        )
    )

    def fake_get(url, headers, timeout):
        env.requests.append((url, headers))
        return folders

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(
        httpx,
        "post",
        _fake_post(env, offer or _response(200, _offer_payload(roots=[ALPHA, BETA]))),
    )


def test_cloud_workspace_join_requires_choosing_folders(env, monkeypatch):
    monkeypatch.setattr(sync_pairing, "is_cloud_workspace", lambda: True)
    _hub_routes(env, monkeypatch)

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.join_hub("mini")

    assert excinfo.value.code == "choose_folders"
    assert "~/Projects/beta" in str(excinfo.value)
    assert not sync_daemon.is_configured()


def test_cloud_workspace_joins_project_only_with_a_subset(env, home, monkeypatch):
    monkeypatch.setattr(sync_pairing, "is_cloud_workspace", lambda: True)
    _hub_routes(env, monkeypatch)

    result = sync_pairing.join_hub("mini", ["~/Projects/beta"])

    config = _config()
    assert config["roots"] == [BETA]
    assert config["placement"]["thin"] is True
    assert "low_water_mb" not in config["placement"]
    assert result["project_only"] is True
    assert (home / "Projects" / "beta").is_dir()
    assert not (home / "Projects" / "alpha").exists()
    assert sync_daemon.read_config_summary()["project_only"] is True


def test_a_laptop_can_opt_into_project_only(env, monkeypatch):
    _hub_routes(env, monkeypatch)

    sync_pairing.join_hub("mini", ["~/Projects/alpha"], project_only=True)

    assert _config()["placement"]["thin"] is True


def test_join_warns_when_the_chosen_folders_exceed_free_disk(env, monkeypatch):
    sizes = [{**ALPHA, "bytes": 3_000_000_000}, {**BETA, "bytes": 40_000_000}]
    _hub_routes(env, monkeypatch, offer=_response(200, _offer_payload(roots=sizes)))
    monkeypatch.setattr(
        sync_pairing,
        "disk_usage",
        lambda path="~": {"free_bytes": 2_000_000_000, "total_bytes": 5_000_000_000},
    )

    result = sync_pairing.join_hub("mini", ["~/Projects/alpha"])
    small = sync_pairing.leave() and sync_pairing.join_hub("mini", ["~/Projects/beta"])

    assert "3.0 GB" in result["warnings"][0] and "2.0 GB free" in result["warnings"][0]
    assert small["warnings"] == []


def test_hub_folders_preview_defaults_by_kind_of_computer(env, monkeypatch):
    _hub_routes(env, monkeypatch)
    monkeypatch.setattr(
        sync_pairing,
        "disk_usage",
        lambda path="~": {"free_bytes": 4_000_000_000, "total_bytes": 5_000_000_000},
    )

    laptop = sync_pairing.hub_folders("mini")
    monkeypatch.setattr(sync_pairing, "is_cloud_workspace", lambda: True)
    cloud = sync_pairing.hub_folders("mini")

    assert env.requests[0] == (
        "http://mini.net.example:18080/api/sync/daemon/pairing/folders/",
        {"Authorization": "Bearer owner-jwt"},
    )
    assert [f["selected"] for f in laptop["folders"]] == [True, True]
    assert laptop["project_only"] is False
    assert [f["selected"] for f in cloud["folders"]] == [False, False]
    assert cloud["project_only"] is True
    assert cloud["this_computer"]["cloud_workspace"] is True
    assert cloud["this_computer"]["disk"]["free_bytes"] == 4_000_000_000
    beta = cloud["folders"][1]
    assert (beta["path"], beta["files"], beta["bytes"]) == (
        "~/Projects/beta",
        80,
        40_000_000,
    )
    # the preview never carries the pair secret
    assert SECRET not in repr(cloud)


def test_hub_folders_preview_falls_back_to_an_older_hubs_offer(env, monkeypatch):
    _hub_routes(env, monkeypatch, folders=_response(404, {}))

    preview = sync_pairing.hub_folders("mini")

    assert [f["path"] for f in preview["folders"]] == [
        "~/Projects/alpha",
        "~/Projects/beta",
    ]
    assert preview["folders"][0]["bytes"] is None
    assert SECRET not in repr(preview)


def test_hub_answers_folders_with_sizes_and_no_secret(env, monkeypatch):
    _write("hub", [ALPHA, BETA])
    monkeypatch.setattr(
        sync_pairing,
        "_root_estimates",
        lambda: {"projects-beta": {"files": 80, "bytes": 40_000_000}},
    )

    response = sync_pairing_api.sync_pairing_folders(
        _request("GET", "/api/sync/daemon/pairing/folders/")
    )

    assert response.status_code == 200
    roots = response.data["roots"]
    # no daemon in tests: no per-project breakdown either
    assert roots[0] == {**ALPHA, "files": None, "bytes": None, "subfolders": None}
    assert roots[1] == {**BETA, "files": 80, "bytes": 40_000_000, "subfolders": None}
    assert SECRET not in repr(response.data)


def test_folders_view_refuses_on_a_non_hub(env):
    _write("edge", [BETA])
    response = sync_pairing_api.sync_pairing_folders(
        _request("GET", "/api/sync/daemon/pairing/folders/")
    )
    assert response.status_code == 409


def test_root_estimates_read_the_daemons_status(env, monkeypatch):
    monkeypatch.setattr(
        sync_daemon.SyncDaemonClient,
        "status",
        lambda self: {
            "roots": [
                {"id": "projects-beta", "entries": 80, "bytes": 40_000_000},
                {"id": "old", "entries": 5},  # an older daemon reports no bytes
            ]
        },
    )

    assert REAL_ROOT_ESTIMATES() == {
        "projects-beta": {"files": 80, "bytes": 40_000_000},
        "old": {"files": 5},
    }


def test_project_only_edge_adds_a_hub_folder_it_did_not_have(env, home, monkeypatch):
    _write("edge", [BETA], thin=True)
    _hub_routes(env, monkeypatch)
    monkeypatch.setattr(httpx, "request", _fake_request(env, _response(200, {})))

    result = sync_pairing.add_root("~/Projects/alpha")

    assert (home / "Projects" / "alpha").is_dir()
    assert [r["path"] for r in sync_daemon.configured_roots()] == [
        "~/Projects/beta",
        "~/Projects/alpha",
    ]
    assert result["peers"] == [{"name": "mini", "ok": True, "error": None}]


def test_edge_still_refuses_a_missing_folder_the_hub_does_not_sync(env, monkeypatch):
    _write("edge", [BETA], thin=True)
    _hub_routes(env, monkeypatch)

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.add_root("~/Projects/typo")

    assert excinfo.value.code == "folder_missing"


def test_project_only_edge_removes_a_folder_here_only_by_default(env, monkeypatch):
    _write("edge", [ALPHA, BETA], thin=True)
    monkeypatch.setattr(
        httpx, "request", lambda *a, **k: pytest.fail("must not touch the hub")
    )

    result = sync_pairing.remove_root("~/Projects/alpha")

    assert result["scope"] == "this_computer"
    assert sync_daemon.configured_roots() == [BETA]


def test_full_edge_removes_here_only_when_asked(env, monkeypatch):
    _write("edge", [ALPHA, BETA])
    monkeypatch.setattr(httpx, "request", _fake_request(env, _response(200, {})))

    here = sync_pairing.remove_root("~/Projects/alpha", scope="this_computer")

    assert here["scope"] == "this_computer"
    assert env.requests == []
    _write("edge", [ALPHA, BETA])
    everywhere = sync_pairing.remove_root("~/Projects/alpha")
    assert everywhere["scope"] == "everywhere"
    assert env.requests[0][0] == "DELETE"


def test_hub_cannot_remove_a_folder_here_only(env):
    _write("hub", [ALPHA, BETA])

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.remove_root("~/Projects/alpha", scope="this_computer")

    assert excinfo.value.code == "hub_holds_all"


def test_remove_here_only_reports_an_unknown_folder(env):
    _write("edge", [ALPHA, BETA], thin=True)

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.remove_root("~/Projects/gamma")

    assert excinfo.value.code == "root_not_found"


def test_available_roots_lists_the_hubs_folders_for_an_edge(env, monkeypatch):
    _write("edge", [BETA], thin=True)
    _hub_routes(env, monkeypatch)

    payload = sync_pairing.available_roots()

    assert payload["project_only"] is True
    assert [(f["path"], f["synced_here"]) for f in payload["folders"]] == [
        ("~/Projects/alpha", False),
        ("~/Projects/beta", True),
    ]


def test_join_view_passes_project_only(env, monkeypatch):
    _hub_routes(env, monkeypatch)

    response = sync_pairing_api.sync_pairing_join(
        _request(
            "POST",
            "/api/sync/daemon/pairing/join/",
            {"hub": "mini", "roots": ["~/Projects/beta"], "project_only": True},
        )
    )
    bad = sync_pairing_api.sync_pairing_join(
        _request(
            "POST",
            "/api/sync/daemon/pairing/join/",
            {"hub": "mini", "project_only": "yes"},
        )
    )

    assert response.status_code == 200 and response.data["project_only"] is True
    assert bad.status_code == 400


def test_roots_view_remove_takes_a_scope(env, monkeypatch):
    _write("edge", [ALPHA, BETA])
    monkeypatch.setattr(httpx, "request", lambda *a, **k: pytest.fail("no hub call"))

    response = sync_pairing_api.sync_daemon_roots(
        _request(
            "DELETE",
            "/api/sync/daemon/roots/",
            {"path": "~/Projects/alpha", "scope": "this_computer"},
        )
    )

    assert response.status_code == 200 and response.data["scope"] == "this_computer"


def test_cli_pair_join_project_only_and_folders(env, monkeypatch):
    _hub_routes(env, monkeypatch)
    monkeypatch.setattr(sync_pairing, "refresh_cloud_registration", lambda **k: None)
    runner = CliRunner()

    preview = runner.invoke(sync_daemon_cli, ["pair", "folders", "mini"])
    joined = runner.invoke(
        sync_daemon_cli,
        ["pair", "join", "mini", "--root", "~/Projects/beta", "--project-only"],
    )

    assert preview.exit_code == 0, preview.output
    assert (
        "~/Projects/alpha" in preview.output and "1200 files, 3.0 GB" in preview.output
    )
    assert joined.exit_code == 0, joined.output
    assert "(project-only)" in joined.output
    assert _config()["placement"]["thin"] is True


def test_cli_remove_folder_here_only(env, monkeypatch):
    _write("edge", [ALPHA, BETA])
    monkeypatch.setattr(httpx, "request", lambda *a, **k: pytest.fail("no hub call"))

    result = CliRunner().invoke(
        sync_daemon_cli,
        ["pair", "remove-folder", "~/Projects/alpha", "--this-computer"],
    )

    assert result.exit_code == 0, result.output
    assert "on this computer" in result.output
    assert sync_daemon.configured_roots() == [BETA]


def test_is_cloud_workspace_uses_markers_then_runtime(monkeypatch):
    from openbase_coder_cli.services import cloud_registration, cloud_workspace

    monkeypatch.setattr(cloud_workspace, "cloud_workspace_id", lambda: "abc123")
    assert sync_pairing.is_cloud_workspace() is True
    monkeypatch.setattr(cloud_workspace, "cloud_workspace_id", lambda: None)
    monkeypatch.setattr(cloud_registration, "runtime_flavor", lambda: "cloud")
    assert sync_pairing.is_cloud_workspace() is True
    monkeypatch.setattr(cloud_registration, "runtime_flavor", lambda: "native")
    assert sync_pairing.is_cloud_workspace() is False
    # A Maritime workspace has no marker files; its environment names it.
    monkeypatch.setenv("MARITIME_AGENT_ID", "agent-1")
    assert sync_pairing.is_cloud_workspace() is True


# --- project-only inside a folder the others sync whole (~/Projects) -------------


def _projects_hub(env, monkeypatch, subfolders=None):
    """A hub syncing ~/Projects whole, with two projects inside."""
    subfolders = (
        subfolders
        if subfolders is not None
        else [
            {
                "name": "app",
                "path": "~/Projects/app",
                "files": 300,
                "bytes": 90_000_000,
            },
            {
                "name": "site",
                "path": "~/Projects/site",
                "files": 40,
                "bytes": 2_000_000,
            },
        ]
    )
    _hub_routes(
        env,
        monkeypatch,
        folders=_response(
            200,
            {
                "roots": [
                    {
                        **PROJECTS,
                        "files": 900_000,
                        "bytes": 80_000_000_000,
                        "subfolders": subfolders,
                    },
                    {**THREADS, "files": 10, "bytes": 1000, "subfolders": []},
                ]
            },
        ),
        offer=_response(200, _offer_payload()),
    )


def test_cloud_workspace_joins_one_project_of_projects(env, home, monkeypatch):
    monkeypatch.setattr(sync_pairing, "is_cloud_workspace", lambda: True)
    _projects_hub(env, monkeypatch)

    result = sync_pairing.join_hub("mini", ["~/Projects/app"])

    assert _config()["roots"] == [{**PROJECTS, "only": ["app"]}]
    assert result["roots"] == [{**PROJECTS, "only": ["app"]}]
    assert (home / "Projects" / "app").is_dir()
    # the 80 GB folder is synced in part: no misleading warning
    assert result["warnings"] == []


def test_join_merges_parts_and_a_whole_folder(env, monkeypatch):
    _projects_hub(env, monkeypatch)

    sync_pairing.join_hub(
        "mini",
        ["~/Projects/site", "~/Projects/app", "~/.openbase/thread-sync"],
        project_only=True,
    )

    assert _config()["roots"] == [{**PROJECTS, "only": ["app", "site"]}, THREADS]


def test_preview_lists_projects_inside_a_folder(env, monkeypatch):
    monkeypatch.setattr(sync_pairing, "is_cloud_workspace", lambda: True)
    _projects_hub(env, monkeypatch)

    preview = sync_pairing.hub_folders("mini")

    projects = preview["folders"][0]
    assert [
        (s["path"], s["files"], s["bytes"], s["selected"])
        for s in projects["subfolders"]
    ] == [
        ("~/Projects/app", 300, 90_000_000, False),
        ("~/Projects/site", 40, 2_000_000, False),
    ]


def test_edge_adds_and_removes_projects_inside_a_folder(env, home, monkeypatch):
    _write("edge", [{**PROJECTS, "only": ["app"]}, THREADS], thin=True)
    _projects_hub(env, monkeypatch)
    monkeypatch.setattr(httpx, "request", lambda *a, **k: pytest.fail("no hub change"))

    added = sync_pairing.add_root("~/Projects/site")
    assert sync_daemon.configured_roots()[0] == {**PROJECTS, "only": ["app", "site"]}
    assert (home / "Projects" / "site").is_dir()
    assert added["peers"] == [] and env.restarted == 1

    with pytest.raises(sync_pairing.PairingError) as again:
        sync_pairing.add_root("~/Projects/app/sub")
    assert again.value.code == "root_overlap"

    with pytest.raises(sync_pairing.PairingError) as everywhere:
        sync_pairing.remove_root("~/Projects/app", scope="everywhere")
    assert everywhere.value.code == "part_of_folder"

    removed = sync_pairing.remove_root("~/Projects/app")
    assert removed["scope"] == "this_computer"
    assert sync_daemon.configured_roots()[0] == {**PROJECTS, "only": ["site"]}
    # the last project of a folder: the folder goes
    sync_pairing.remove_root("~/Projects/site")
    assert sync_daemon.configured_roots() == [THREADS]


def test_edge_adds_a_project_of_a_folder_it_did_not_sync(env, home, monkeypatch):
    _write("edge", [THREADS], thin=True)
    _projects_hub(env, monkeypatch)

    sync_pairing.add_root("~/Projects/app")

    assert sync_daemon.configured_roots() == [THREADS, {**PROJECTS, "only": ["app"]}]


def test_full_edge_adding_inside_a_whole_folder_is_an_overlap(env, home, monkeypatch):
    _write("edge", [PROJECTS])
    (home / "Projects" / "app").mkdir(parents=True)
    _projects_hub(env, monkeypatch)

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.add_root("~/Projects/app")

    assert excinfo.value.code == "root_overlap"


def test_available_roots_marks_projects_synced_here(env, monkeypatch):
    _write("edge", [{**PROJECTS, "only": ["app"]}], thin=True)
    _projects_hub(env, monkeypatch)

    payload = sync_pairing.available_roots()

    projects, threads = payload["folders"]
    assert projects["synced_here"] is False and projects["partly_synced_here"] is True
    assert [(s["name"], s["synced_here"]) for s in projects["subfolders"]] == [
        ("app", True),
        ("site", False),
    ]
    assert threads["synced_here"] is False


def test_subfolders_come_from_the_daemons_folder_sizes(env, monkeypatch):
    monkeypatch.setattr(
        sync_daemon.SyncDaemonClient,
        "folder_sizes",
        lambda self, root: [
            {"name": "app", "dir": True, "files": 3, "bytes": 30},
            {"name": "README.md", "dir": False, "files": 1, "bytes": 5},
        ],
    )

    assert sync_pairing._subfolders(PROJECTS) == [
        {"name": "app", "path": "~/Projects/app", "files": 3, "bytes": 30}
    ]


def test_cli_pair_folders_lists_projects(env, monkeypatch):
    _projects_hub(env, monkeypatch)

    result = CliRunner().invoke(sync_daemon_cli, ["pair", "folders", "mini"])

    assert result.exit_code == 0, result.output
    assert "~/Projects/app" in result.output and "300 files, 90.0 MB" in result.output


def test_only_is_written_and_read_back(env):
    _write("edge", [{**PROJECTS, "only": ["app", "site"]}])

    assert _config()["roots"][0]["only"] == ["app", "site"]
    assert sync_daemon.read_config_summary()["roots"][0]["only"] == ["app", "site"]


def test_project_only_needs_an_engine_that_supports_it(env, home, monkeypatch):
    """An older engine ignores only/thin and keeps 10 GB free: refuse rather
    than let a small cloud workspace try to take all of ~/Projects."""
    monkeypatch.setattr(sync_pairing, "engine_features", lambda: set())
    monkeypatch.setattr(sync_pairing, "is_cloud_workspace", lambda: True)
    _projects_hub(env, monkeypatch)

    with pytest.raises(sync_pairing.PairingError) as join:
        sync_pairing.join_hub("mini", ["~/Projects/app"])
    assert join.value.code == "engine_outdated"
    assert not sync_daemon.is_configured()

    # a full copy of whole folders still works with an older engine
    monkeypatch.setattr(sync_pairing, "is_cloud_workspace", lambda: False)
    sync_pairing.join_hub("mini", project_only=False)
    assert _config()["roots"] == [PROJECTS, THREADS]


def test_adding_a_project_needs_a_project_only_engine(env, monkeypatch):
    _write("edge", [{**PROJECTS, "only": ["app"]}], thin=True)
    _projects_hub(env, monkeypatch)
    monkeypatch.setattr(sync_pairing, "engine_features", lambda: set())

    with pytest.raises(sync_pairing.PairingError) as excinfo:
        sync_pairing.add_root("~/Projects/site")

    assert excinfo.value.code == "engine_outdated"
    assert sync_daemon.configured_roots() == [{**PROJECTS, "only": ["app"]}]


def test_engine_features_runs_the_daemon_binary(monkeypatch, tmp_path):
    from openbase_coder_cli.services import installation, launchd

    binary = tmp_path / "openbase-syncd"
    binary.write_text(
        "#!/bin/sh\n[ \"$1\" = --features ] && printf 'disk-aware\\nproject-only\\n' && exit 0\nexit 2\n"
    )
    binary.chmod(0o755)
    monkeypatch.setattr(
        installation.InstallationConfig, "load", classmethod(lambda cls: None)
    )
    monkeypatch.setattr(
        launchd,
        "_binary_resolvers",
        lambda config: {"openbase_syncd": lambda: str(binary)},
    )
    assert REAL_ENGINE_FEATURES() == {"disk-aware", "project-only"}

    old = tmp_path / "old-syncd"
    old.write_text(
        "#!/bin/sh\necho 'flag provided but not defined: -features' >&2\nexit 2\n"
    )
    old.chmod(0o755)
    monkeypatch.setattr(
        launchd,
        "_binary_resolvers",
        lambda config: {"openbase_syncd": lambda: str(old)},
    )
    assert REAL_ENGINE_FEATURES() == set()
