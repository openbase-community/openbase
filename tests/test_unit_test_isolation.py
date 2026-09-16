"""Regression coverage for the unit suite's production isolation boundary."""

import socket
from pathlib import Path

import httpx
import pytest

from openbase_coder_cli import paths
from openbase_coder_cli.config import machine_token_manager, token_manager


def test_credentials_are_temporary_and_empty(tmp_path):
    assert paths.OPENBASE_BASE_DIR != Path.home() / ".openbase"
    assert token_manager.AUTH_JSON_PATH == tmp_path / "auth.json"
    assert (
        machine_token_manager.MACHINE_TOKEN_JSON_PATH == tmp_path / "machine-token.json"
    )
    assert not token_manager.AUTH_JSON_PATH.exists()
    assert not machine_token_manager.MACHINE_TOKEN_JSON_PATH.exists()
    assert not token_manager.TokenManager("https://example.com").has_refresh_token
    assert token_manager._instance is None


def test_database_is_inside_temporary_installation():
    from openbase_coder_cli.config import settings

    assert Path(settings.DATABASES["default"]["NAME"]).parent == paths.OPENBASE_BASE_DIR


def test_unmocked_http_request_fails_before_network_io():
    with pytest.raises(pytest.fail.Exception, match="cannot access external networks"):
        httpx.post(
            "https://example.com/api/provider",
            json={"provider": "tailscale"},
            trust_env=False,
        )


@pytest.mark.parametrize("operation", ["connect", "connect_ex", "sendto"])
@pytest.mark.parametrize(
    "family, address",
    [(socket.AF_INET, ("192.0.2.1", 443)), (socket.AF_INET6, ("2001:db8::1", 443))],
)
def test_literal_ip_cannot_bypass_network_guard(operation, family, address):
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        with pytest.raises(
            pytest.fail.Exception, match="cannot access external networks"
        ):
            if operation == "sendto":
                sock.sendto(b"test", address)
            else:
                getattr(sock, operation)(address)
