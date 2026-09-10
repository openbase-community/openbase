"""Security invariants for isolated Maritime workspace bootstrap."""

import json
from importlib import import_module
from pathlib import Path
from unittest import mock

import httpx
import pytest

from openbase_coder_cli.config import machine_token_manager as mt_module

provision_module = import_module("openbase_coder_cli.cli.provision")


@pytest.fixture
def bootstrap_paths(tmp_path, monkeypatch):
    machine_token = tmp_path / "machine-token.json"
    owner_identity = tmp_path / "owner-identity.json"
    netmesh_key = tmp_path / "bootstrap-netmesh-authkey"
    monkeypatch.setattr(mt_module, "MACHINE_TOKEN_JSON_PATH", machine_token)
    monkeypatch.setattr(provision_module, "OWNER_IDENTITY_JSON_PATH", owner_identity)
    monkeypatch.setattr(provision_module, "NETMESH_AUTHKEY_FILE", netmesh_key)
    return machine_token, owner_identity, netmesh_key


def bootstrap_response(*, scopes=None):
    return httpx.Response(
        200,
        json={
            "machine_token": "obmt_workspace",
            "machine_token_prefix": "obmt_workspace",
            "machine_token_install_id": "maritime-devspace-123",
            "machine_token_scopes": scopes or ["llm_proxy", "audio_proxy"],
            "owner": {"sub": "7", "email": "owner@example.com"},
            "netmesh": {
                "control_url": "https://net.example.com",
                "auth_key": "single-use-enrollment",
            },
        },
        request=httpx.Request("POST", "https://backend.example.com"),
    )


def test_exchange_persists_only_scoped_install_credentials(bootstrap_paths):
    machine_token, owner_identity, netmesh_key = bootstrap_paths

    with mock.patch.object(httpx, "post", return_value=bootstrap_response()) as post:
        netmesh = provision_module._exchange_bootstrap(
            "obmb_one-time", "https://backend.example.com"
        )

    assert post.call_args.kwargs["headers"] == {
        "Authorization": "Openbase-Bootstrap obmb_one-time"
    }
    assert netmesh["control_url"] == "https://net.example.com"
    saved = json.loads(machine_token.read_text())
    assert saved["scopes"] == ["llm_proxy", "audio_proxy"]
    assert json.loads(owner_identity.read_text()) == {
        "sub": "7",
        "email": "owner@example.com",
    }
    assert netmesh_key.read_text() == "single-use-enrollment"
    for path in (machine_token, owner_identity, netmesh_key):
        assert path.stat().st_mode & 0o777 == 0o600
    assert not (machine_token.parent / "auth.json").exists()


def test_exchange_rejects_broader_scopes_before_writing(bootstrap_paths):
    machine_token, owner_identity, netmesh_key = bootstrap_paths

    with (
        mock.patch.object(
            httpx,
            "post",
            return_value=bootstrap_response(
                scopes=["llm_proxy", "audio_proxy", "workspace_admin"]
            ),
        ),
        pytest.raises(provision_module.click.ClickException),
    ):
        provision_module._exchange_bootstrap(
            "obmb_one-time", "https://backend.example.com"
        )

    assert not machine_token.exists()
    assert not owner_identity.exists()
    assert not netmesh_key.exists()


def test_exchange_rejects_insecure_backend_before_sending(bootstrap_paths):
    with (
        mock.patch.object(httpx, "post") as post,
        pytest.raises(
            provision_module.click.ClickException,
            match="HTTPS backend",
        ),
    ):
        provision_module._exchange_bootstrap(
            "obmb_one-time", "http://backend.example.com"
        )

    post.assert_not_called()


def test_container_entrypoint_enforces_private_durable_runtime():
    root = Path(__file__).parents[1]
    entrypoint = (root / "docker" / "entrypoint.sh").read_text()
    dockerfile = (root / "Dockerfile").read_text()

    assert "OPENBASE_CODER_CLI_HOST:-127.0.0.1" in entrypoint
    assert "Refusing to run the Maritime workspace as root" in entrypoint
    assert "Maritime state must live below /data" in entrypoint
    assert "Maritime projects must live below /data" in entrypoint
    assert "FROM golang:1.26.5-bookworm AS tunneld-build" in dockerfile
    assert "COPY --from=tunneld-build" in dockerfile
    assert 'VOLUME ["/home/openbase/.openbase", "/data"]' in dockerfile


def test_container_entrypoint_maritime_selects_netmesh():
    """Maritime must never silently boot the tailscale path (PRD R1)."""
    root = Path(__file__).parents[1]
    entrypoint = (root / "docker" / "entrypoint.sh").read_text()

    # Maritime defaults to netmesh, and the PRD's canonical "netmesh" value
    # is accepted as an alias for the internal provider id.
    assert 'NETWORK_MODE="${OPENBASE_CODER_NETWORK_MODE:-netmesh}"' in entrypoint
    assert 'if [ "$NETWORK_MODE" = "netmesh" ]; then' in entrypoint
    assert 'NETWORK_MODE="netmesh-tsnet"' in entrypoint
    # Netmesh mode runs LiveKit in the loopback-candidate netmesh profile,
    # not tailscale (and not bare "local", which disables ICE-TCP).
    assert "LIVEKIT_NETWORK_MODE=netmesh" in entrypoint


def test_container_entrypoint_retains_staged_key_until_enrolled():
    """The single-use netmesh key survives failed enrollments (PRD R1).

    Boot-time egress lag or a restart before login must not strand the node:
    the staged key is only removed once the daemon reports an enrolled,
    forwarding state.
    """
    root = Path(__file__).parents[1]
    entrypoint = (root / "docker" / "entrypoint.sh").read_text()

    read_at = entrypoint.index('netmesh_authkey="$(/bin/cat "$NETMESH_AUTHKEY_FILE")"')
    remove_at = entrypoint.index('rm -f "$NETMESH_AUTHKEY_FILE"')
    confirm_at = entrypoint.index("h.get('backend_state') == 'Running'")
    assert read_at < confirm_at < remove_at
    assert "h.get('forwards_up')" in entrypoint


def test_container_entrypoint_keeps_backend_homes_durable():
    """Both coding-backend logins survive container recreation (PRD R3)."""
    root = Path(__file__).parents[1]
    entrypoint = (root / "docker" / "entrypoint.sh").read_text()

    assert 'ln -s "$DATA_DIR/normal-codex-home" "$HOME/.codex"' in entrypoint
    assert (
        'export CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$DATA_DIR/normal-claude-home}"'
        in entrypoint
    )
    assert 'ln -s "$CLAUDE_CONFIG_DIR" "$HOME/.claude"' in entrypoint
    assert 'ln -s "$CLAUDE_CONFIG_DIR/.claude.json" "$HOME/.claude.json"' in entrypoint


def test_container_projects_dir_prefers_durable_workspace_dir(monkeypatch):
    monkeypatch.delenv("OPENBASE_CODER_WORKSPACE_DIR", raising=False)
    monkeypatch.delenv("OPENBASE_CODER_PROJECTS_DIR", raising=False)
    assert provision_module._container_projects_dir() == "/data/workspace"

    # The image ENV pins OPENBASE_CODER_WORKSPACE_DIR to an image-layer path;
    # that must never win over the durable default.
    monkeypatch.setenv("OPENBASE_CODER_WORKSPACE_DIR", "/opt/openbase-coder/workspace")
    assert provision_module._container_projects_dir() == "/data/workspace"

    monkeypatch.setenv("OPENBASE_CODER_PROJECTS_DIR", "/data/projects-alt")
    assert provision_module._container_projects_dir() == "/data/projects-alt"

    monkeypatch.setenv("OPENBASE_CODER_WORKSPACE_DIR", "/data/Projects")
    assert provision_module._container_projects_dir() == "/data/Projects"
