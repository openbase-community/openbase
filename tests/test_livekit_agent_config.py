from __future__ import annotations

from pathlib import Path

import pytest

from openbase_coder_cli.livekit_agent import config


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("ws://127.0.0.1:4500", "unix://"),
        ("wss://codex.example/rpc", "wss://codex.example/rpc"),
    ],
)
def test_openbase_env_refresh_keeps_managed_codex_endpoint_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    expected: str,
) -> None:
    canonical_env = tmp_path / "openbase.env"
    canonical_env.write_text(
        f"CODEX_APP_SERVER_URL={configured}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config, "_canonical_env_path", lambda: canonical_env)
    monkeypatch.delenv("CODEX_APP_SERVER_URL", raising=False)

    config._load_openbase_env(override=True)

    assert config.os.environ["CODEX_APP_SERVER_URL"] == expected
