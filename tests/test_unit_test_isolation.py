"""Regression coverage for the unit suite's production isolation boundary."""

import os
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import httpx
import pytest

from openbase_coder_cli import paths
from openbase_coder_cli.config import machine_token_manager, token_manager


@pytest.mark.parametrize("inherited_homes", [False, True])
@pytest.mark.parametrize("outcome", ["pass", "test_failure", "collection_failure"])
def test_agent_home_isolation_and_cleanup(tmp_path, inherited_homes, outcome):
    """Run the real suite bootstrap without risking the caller's agent homes."""
    user_home = tmp_path / "user"
    user_home.mkdir()
    variables = {
        "OPENBASE_CODER_CLI_DATA_DIR": user_home / ".openbase",
        "CODEX_HOME": user_home / ".codex",
        "CLAUDE_CONFIG_DIR": user_home / ".claude",
    }
    env = dict(os.environ, HOME=str(user_home), USERPROFILE=str(user_home))
    for variable, default in variables.items():
        # Both default locations and explicit inherited overrides must survive.
        override = user_home / f"custom-{default.name}"
        for directory in (default, override):
            directory.mkdir()
            for filename in ("config.toml", "settings.json", ".claude.json"):
                (directory / filename).write_text("# untouched user settings\n")
        env.pop(variable, None)
        if inherited_homes:
            env[variable] = str(override)
    (user_home / ".claude.json").write_text('{"personal": true}\n')
    before = {
        p.relative_to(user_home): p.read_bytes()
        for p in user_home.rglob("*")
        if p.is_file()
    }

    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copyfile(Path(__file__).with_name("conftest.py"), suite / "conftest.py")
    (suite / "test_probe.py").write_text(
        textwrap.dedent(f"""
        import os
        from pathlib import Path
        from openbase_coder_cli import paths
        from openbase_coder_cli.cli.setup import hooks

        # Exercise writes during collection, before autouse fixtures can help.
        root = paths.OPENBASE_BASE_DIR
        assert root != Path.home() / ".openbase"
        for path in (paths.CODEX_CONFIG_PATH, paths.CLAUDE_SETTINGS_PATH,
                     paths.CLAUDE_STATE_PATH, paths.CLAUDE_INBOX_REGISTRY_DIR):
            assert path.is_relative_to(root)
        assert Path(os.environ["CODEX_HOME"]) == paths.CODEX_HOME_DIR
        assert Path(os.environ["CLAUDE_CONFIG_DIR"]) == paths.CLAUDE_CONFIG_DIR
        hooks.ensure_session_id_hook_script()
        hooks.ensure_default_session_id_hooks()
        assert paths.CODEX_CONFIG_PATH.is_file()
        assert paths.CLAUDE_SETTINGS_PATH.is_file()
        paths.CLAUDE_STATE_PATH.write_text('{{"test": true}}')
        Path({str(tmp_path / "test-root")!r}).write_text(str(root))
        if {outcome!r} == "collection_failure":
            raise RuntimeError("intentional collection failure")

        def test_probe():
            assert {outcome!r} != "test_failure", "intentional test failure"
    """)
    )
    script = textwrap.dedent("""
        import os, sys
        from pathlib import Path
        import pytest

        variables = ("OPENBASE_CODER_CLI_DATA_DIR", "CODEX_HOME", "CLAUDE_CONFIG_DIR")
        original = {key: os.environ.get(key) for key in variables}
        result = pytest.main([sys.argv[1], "-q", "--confcutdir=" + sys.argv[1]])
        assert {key: os.environ.get(key) for key in variables} == original
        root = Path(Path(sys.argv[2]).read_text())
        assert not root.exists(), "temporary hooks and agent settings leaked"
        assert result == int(sys.argv[3]), result
    """)
    expected_exit = {"pass": 0, "test_failure": 1, "collection_failure": 2}[outcome]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(suite),
            str(tmp_path / "test-root"),
            str(expected_exit),
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    after = {
        p.relative_to(user_home): p.read_bytes()
        for p in user_home.rglob("*")
        if p.is_file()
    }
    assert after == before


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
