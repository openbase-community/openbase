"""Optional AI account linking (Codex / Claude Code) for a workspace."""

from __future__ import annotations

import os
import stat
import time

import pytest

os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings")

import django  # noqa: E402
from rest_framework.test import APIRequestFactory, force_authenticate  # noqa: E402

django.setup()

from openbase_coder_cli import ai_account  # noqa: E402
from openbase_coder_cli.openbase_coder_cli_app import ai_account_views  # noqa: E402


def _fake_cli(tmp_path, body: str):
    path = tmp_path / "fake-login"
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


def _wait(manager, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snapshot = manager.current()
        if snapshot and predicate(snapshot):
            return snapshot
        time.sleep(0.05)
    raise AssertionError(f"timed out; last {manager.current()}")


@pytest.fixture
def manager(monkeypatch, tmp_path):
    marker = tmp_path / "linked"
    monkeypatch.setattr(ai_account, "is_linked", lambda provider: marker.exists())
    monkeypatch.setattr(
        ai_account, "_login_env", lambda: dict(os.environ, MARKER=str(marker))
    )
    manager = ai_account.LoginManager()
    yield manager
    manager.cancel()


def test_codex_login_reports_the_url_and_success(manager, monkeypatch, tmp_path):
    cli = _fake_cli(
        tmp_path,
        'echo "Starting local login server on http://localhost:1455."\n'
        'echo "If your browser did not open, navigate to this URL:"\n'
        'printf "\\033[1mhttps://auth.openai.com/oauth/authorize?state=abc\\033[0m\\n"\n'
        'sleep 0.3\ntouch "$MARKER"\necho "Successfully logged in"\n',
    )
    monkeypatch.setattr(ai_account, "_command", lambda provider: [cli])
    manager.start(ai_account.CODEX)
    waiting = _wait(manager, lambda s: s["url"])
    assert waiting["url"] == "https://auth.openai.com/oauth/authorize?state=abc"
    assert waiting["needs_code"] is False
    done = _wait(manager, lambda s: s["state"] in ("succeeded", "failed"))
    assert done["state"] == "succeeded"
    assert "linked" in done["message"]


def test_claude_login_takes_a_pasted_code(manager, monkeypatch, tmp_path):
    cli = _fake_cli(
        tmp_path,
        'echo "Browser didn\'t open? Use the url below to sign in:"\n'
        'echo "https://claude.ai/oauth/authorize?code=true&state=s"\n'
        'printf "Paste code here if prompted > "\n'
        'read code\n[ "$code" = "abc#def" ] && touch "$MARKER"\n',
    )
    monkeypatch.setattr(ai_account, "_command", lambda provider: [cli])
    manager.start(ai_account.CLAUDE_CODE)
    prompted = _wait(manager, lambda s: s["needs_code"])
    assert prompted["url"].startswith("https://claude.ai/oauth/authorize")

    with pytest.raises(ValueError):
        manager.submit_code("bad\ncode")
    sent = manager.submit_code("  abc#def ")
    assert sent["needs_code"] is False
    done = _wait(manager, lambda s: s["state"] in ("succeeded", "failed"))
    assert done["state"] == "succeeded"


def test_failed_login_and_cancel(manager, monkeypatch, tmp_path):
    monkeypatch.setattr(
        ai_account, "_command", lambda provider: [_fake_cli(tmp_path, "exit 3\n")]
    )
    manager.start(ai_account.CODEX)
    done = _wait(manager, lambda s: s["state"] == "failed")
    assert "exit 3" in done["message"]

    monkeypatch.setattr(
        ai_account, "_command", lambda provider: [_fake_cli(tmp_path, "sleep 30\n")]
    )
    manager.start(ai_account.CODEX)
    manager.cancel()
    assert manager.current()["state"] == "cancelled"
    with pytest.raises(ValueError):
        manager.submit_code("x")


def test_missing_cli_is_reported(manager, monkeypatch):
    monkeypatch.setattr(ai_account, "_command", lambda provider: ["/nonexistent/codex"])
    with pytest.raises(RuntimeError, match="not available"):
        manager.start(ai_account.CODEX)


def test_select_requires_a_linked_account_and_restarts(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("OPENBASE_CODING_BACKEND=openbase_cloud\n")
    import importlib

    backend_cli = importlib.import_module("openbase_coder_cli.cli.backend")
    monkeypatch.setattr(ai_account, "DEFAULT_ENV_FILE_PATH", env_file)
    restarts = []
    monkeypatch.setattr(
        backend_cli, "schedule_backend_restart", lambda: restarts.append(1)
    )
    linked = {"codex": False}
    monkeypatch.setattr(
        ai_account, "is_linked", lambda p: p == "openbase_cloud" or linked.get(p, False)
    )

    assert ai_account.selected_choice() == "openbase_cloud"
    with pytest.raises(ValueError, match="Link your"):
        ai_account.select("codex")
    linked["codex"] = True
    assert ai_account.select("codex") is True
    text = env_file.read_text()
    assert "OPENBASE_CODING_BACKEND=codex" in text
    assert "OPENBASE_CODING_BACKENDS=codex,openbase_cloud" in text
    assert ai_account.selected_choice() == "codex"
    assert ai_account.select("codex") is False
    assert ai_account.select("openbase_cloud") is True
    assert ai_account.selected_choice() == "openbase_cloud"
    assert restarts == [1, 1]


def test_select_drops_role_models_of_the_other_engine(monkeypatch, tmp_path):
    from openbase_coder_cli import dispatcher_config

    config = tmp_path / "dispatcher.json"
    monkeypatch.setattr(dispatcher_config, "CODEX_DISPATCHER_CONFIG_PATH", config)
    monkeypatch.setattr("openbase_coder_cli.paths.CODEX_DISPATCHER_CONFIG_PATH", config)
    engines = {"claude-sonnet": "claude", "gpt-codex": "codex"}
    monkeypatch.setattr(dispatcher_config, "model_engine", lambda m: engines.get(m))
    dispatcher_config._write_dispatcher_config(
        {"role_models": {"dispatcher": "claude-sonnet", "super_agents": "gpt-codex"}},
        config,
    )
    ai_account._align_role_models(ai_account.CODEX)
    assert dispatcher_config.read_dispatcher_config(config)["role_models"] == {
        "super_agents": "gpt-codex"
    }
    ai_account._align_role_models(ai_account.CLAUDE_CODE)
    assert dispatcher_config.read_dispatcher_config(config)["role_models"] == {}


def test_view_status_and_validation(monkeypatch):
    monkeypatch.setattr(ai_account, "is_linked", lambda p: p == "openbase_cloud")
    monkeypatch.setattr(ai_account, "selected_choice", lambda: "openbase_cloud")
    factory = APIRequestFactory()
    request = factory.get("/api/settings/ai-account/")
    force_authenticate(request, user=type("U", (), {"is_authenticated": True})())
    response = ai_account_views.ai_account_settings(request)
    assert response.status_code == 200
    assert response.data["selected"] == "openbase_cloud"
    assert [o["id"] for o in response.data["options"]] == [
        "openbase_cloud",
        "codex",
        "claude_code",
    ]
    assert response.data["options"][1]["linked"] is False

    for body in (
        {"action": "link", "provider": "openbase_cloud"},
        {"action": "link"},
        {"action": "code"},
        {"action": "select"},
        {"action": "nope"},
    ):
        request = factory.post("/api/settings/ai-account/", body, format="json")
        force_authenticate(request, user=type("U", (), {"is_authenticated": True})())
        assert ai_account_views.ai_account_settings(request).status_code == 400, body

    request = factory.post(
        "/api/settings/ai-account/",
        {"action": "select", "provider": "codex"},
        format="json",
    )
    force_authenticate(request, user=type("U", (), {"is_authenticated": True})())
    response = ai_account_views.ai_account_settings(request)
    assert response.status_code == 400
    assert "Link your" in response.data["error"]


def test_linked_account_reads_the_codex_id_token(monkeypatch, tmp_path):
    import base64
    import json

    claims = (
        base64.urlsafe_b64encode(json.dumps({"email": "dev@example.com"}).encode())
        .decode()
        .rstrip("=")
    )
    (tmp_path / "auth.json").write_text(
        json.dumps({"tokens": {"id_token": f"h.{claims}.s"}})
    )
    monkeypatch.setattr(ai_account, "CODEX_HOME_DIR", tmp_path)
    assert ai_account.linked_account(ai_account.CODEX) == "dev@example.com"
    (tmp_path / "auth.json").write_text("{}")
    assert ai_account.linked_account(ai_account.CODEX) is None


def test_status_reports_whether_the_cli_is_available(monkeypatch):
    monkeypatch.setattr(ai_account, "is_linked", lambda p: p == "openbase_cloud")
    monkeypatch.setattr(ai_account, "selected_choice", lambda: "openbase_cloud")
    monkeypatch.setattr(ai_account, "find_backend_binary", lambda name: None)
    monkeypatch.setattr(
        ai_account.shutil,
        "which",
        lambda name: "/usr/bin/claude" if name == "claude" else None,
    )
    options = {o["id"]: o for o in ai_account.status()["options"]}
    assert options["openbase_cloud"]["available"] is True
    assert options["codex"]["available"] is False
    assert options["claude_code"]["available"] is True


def test_status_reports_accounts_that_need_a_relink(monkeypatch):
    from openbase_coder_cli import backend_auth

    monkeypatch.setattr(ai_account, "is_linked", lambda p: p != "claude_code")
    monkeypatch.setattr(ai_account, "linked_account", lambda p: None)
    monkeypatch.setattr(ai_account, "selected_choice", lambda: "claude_code")
    backend_auth.mark_relink_needed("codex")
    options = {o["id"]: o for o in ai_account.status()["options"]}
    assert options["openbase_cloud"]["needs_relink"] is False
    # Its login failed during a turn.
    assert options["codex"]["needs_relink"] is True
    # In use without a login.
    assert options["claude_code"]["needs_relink"] is True


def test_macos_login_url_is_sent_to_the_phone(manager, monkeypatch, tmp_path):
    cli = _fake_cli(
        tmp_path,
        'echo "Open https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455"\nsleep 1\n',
    )
    monkeypatch.setattr(ai_account, "_command", lambda provider: [cli])
    opened = []
    monkeypatch.setattr(ai_account, "browser_env_ignored", lambda: True)
    monkeypatch.setattr(ai_account, "_open_on_phone", opened.append)
    manager.start(ai_account.CODEX)
    _wait(manager, lambda s: s["url"])
    assert opened == [
        "https://auth.openai.com/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A1455"
    ]


def test_linux_relies_on_the_browser_shim(manager, monkeypatch, tmp_path):
    cli = _fake_cli(tmp_path, 'echo "Open https://example.com/auth"\nsleep 1\n')
    monkeypatch.setattr(ai_account, "_command", lambda provider: [cli])
    opened = []
    monkeypatch.setattr(ai_account, "browser_env_ignored", lambda: False)
    monkeypatch.setattr(ai_account, "_open_on_phone", opened.append)
    manager.start(ai_account.CODEX)
    _wait(manager, lambda s: s["url"])
    assert opened == []


@pytest.mark.parametrize("outcome", ["cancel", "fail"])
def test_an_unfinished_relink_keeps_the_existing_login(
    manager, monkeypatch, tmp_path, outcome
):
    auth = tmp_path / "codex-home" / "auth.json"
    auth.parent.mkdir()
    auth.write_text('{"tokens": "old"}')
    monkeypatch.setattr(ai_account, "_credential_paths", lambda provider: [auth])
    # Like `codex login`: drop the stored login first, then wait for the browser.
    exit_line = "sleep 30\n" if outcome == "cancel" else "exit 1\n"
    cli = _fake_cli(
        tmp_path, f'rm -f "{auth}"\necho "Open https://auth.openai.com/x"\n{exit_line}'
    )
    monkeypatch.setattr(ai_account, "_command", lambda provider: [cli])
    monkeypatch.setattr(ai_account, "browser_env_ignored", lambda: False)
    manager.start(ai_account.CODEX)
    if outcome == "cancel":
        _wait(manager, lambda s: s["url"])
        assert not auth.exists()
        manager.cancel()
        _wait(manager, lambda s: auth.exists())
    else:
        _wait(manager, lambda s: s["state"] == "failed")
    assert auth.read_text() == '{"tokens": "old"}'


def test_a_successful_relink_keeps_the_new_login(manager, monkeypatch, tmp_path):
    auth = tmp_path / "auth.json"
    auth.write_text('{"tokens": "old"}')
    monkeypatch.setattr(ai_account, "_credential_paths", lambda provider: [auth])
    cli = _fake_cli(
        tmp_path,
        f'echo \'{{"tokens": "new"}}\' > "{auth}"\ntouch "$MARKER"\necho "Open https://auth.openai.com/x"\n',
    )
    monkeypatch.setattr(ai_account, "_command", lambda provider: [cli])
    monkeypatch.setattr(ai_account, "browser_env_ignored", lambda: False)
    manager.start(ai_account.CODEX)
    _wait(manager, lambda s: s["state"] == "succeeded")
    assert "new" in auth.read_text()
