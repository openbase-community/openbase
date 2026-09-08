from __future__ import annotations

import importlib
import json

from click.testing import CliRunner

from openbase_coder_cli import claude_auth

claude_cli = importlib.import_module("openbase_coder_cli.cli.claude")


def test_claude_status_guides_login_when_not_authenticated(monkeypatch) -> None:
    monkeypatch.setattr(
        claude_cli,
        "verified_claude_auth_status",
        lambda: claude_auth.ClaudeAuthStatus(
            logged_in=False,
            raw_output='{"loggedIn": false}',
            returncode=0,
        ),
    )

    result = CliRunner().invoke(claude_cli.claude, ["status"])

    assert result.exit_code != 0
    assert "claude login" in result.output


def test_is_claude_auth_failure_text_matches_turn_failures() -> None:
    assert claude_auth.is_claude_auth_failure_text(
        "Failed to authenticate. API Error: 401 Invalid bearer token"
    )
    assert claude_auth.is_claude_auth_failure_text(
        "Failed to authenticate: OAuth session expired and could not be refreshed"
    )
    assert claude_auth.is_claude_auth_failure_text("Not logged in · Please run /login")
    assert not claude_auth.is_claude_auth_failure_text("")
    assert not claude_auth.is_claude_auth_failure_text(None)
    assert not claude_auth.is_claude_auth_failure_text(
        "The tests failed to authenticate against the staging backend."
    )


def _status(logged_in: bool, output: str = "") -> claude_auth.ClaudeAuthStatus:
    return claude_auth.ClaudeAuthStatus(
        logged_in=logged_in, raw_output=output, returncode=0
    )


def test_verified_status_trusts_unexpired_credentials(monkeypatch) -> None:
    monkeypatch.setattr(
        claude_auth, "claude_auth_status", lambda **_: _status(True, "cached")
    )
    monkeypatch.setattr(
        claude_auth,
        "read_claude_credential_expiry",
        lambda *_: (claude_auth.time.time() + 3600) * 1000,
    )

    def _no_probe(**_kwargs):
        raise AssertionError("probe must not run for unexpired credentials")

    monkeypatch.setattr(claude_auth, "probe_claude_auth", _no_probe)

    assert claude_auth.verified_claude_auth_status().logged_in is True


def test_verified_status_reports_logout_when_expired_probe_fails(monkeypatch) -> None:
    failure = "Failed to authenticate: OAuth session expired and could not be refreshed"
    monkeypatch.setattr(
        claude_auth, "claude_auth_status", lambda **_: _status(True, "cached")
    )
    monkeypatch.setattr(
        claude_auth, "read_claude_credential_expiry", lambda *_: 1.0
    )
    monkeypatch.setattr(
        claude_auth, "probe_claude_auth", lambda **_: _status(False, failure)
    )

    result = claude_auth.verified_claude_auth_status()

    assert result.logged_in is False
    assert result.raw_output == failure


def test_verified_status_keeps_login_when_probe_refreshes(monkeypatch) -> None:
    monkeypatch.setattr(
        claude_auth, "claude_auth_status", lambda **_: _status(True, "cached")
    )
    monkeypatch.setattr(
        claude_auth, "read_claude_credential_expiry", lambda *_: 1.0
    )
    monkeypatch.setattr(
        claude_auth, "probe_claude_auth", lambda **_: _status(True, "ok")
    )

    result = claude_auth.verified_claude_auth_status()

    assert result.logged_in is True
    assert result.raw_output == "cached"


def test_read_credential_expiry_from_credentials_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(claude_auth.platform, "system", lambda: "Linux")
    config_dir = tmp_path / "claude_config"
    config_dir.mkdir()
    (config_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": 1752000000000}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_auth, "CLAUDE_CONFIG_DIR", config_dir)

    assert claude_auth.read_claude_credential_expiry() == 1752000000000

    monkeypatch.setattr(claude_auth, "CLAUDE_CONFIG_DIR", tmp_path / "missing")
    assert claude_auth.read_claude_credential_expiry() is None
