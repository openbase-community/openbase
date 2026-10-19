"""A backend switch takes effect in place: no django-cli or voice worker restart."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from openbase_coder_cli import backend_config
from openbase_coder_cli.livekit_agent import livekit
from openbase_coder_cli.thread_sync import session_manager as session_manager_module


def _env_file(tmp_path: Path, monkeypatch, text: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(text, encoding="utf-8")
    monkeypatch.setattr("openbase_coder_cli.paths.DEFAULT_ENV_FILE_PATH", env_file)
    monkeypatch.setattr(backend_config, "_SYNCED_ENV_FILE_STAMP", None)
    return env_file


def test_env_file_backend_keys_are_copied_into_the_process_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "openbase_cloud")
    monkeypatch.delenv("OPENBASE_CODING_BACKENDS", raising=False)
    env_file = _env_file(
        tmp_path,
        monkeypatch,
        "OPENBASE_CODING_BACKEND=codex\nOPENBASE_CODING_BACKENDS=codex,openbase_cloud\n",
    )

    assert backend_config.sync_process_env_from_env_file() is True
    assert backend_config.configured_execution_backends() == ["codex", "claude_code"]

    # Unchanged file: nothing to do, and the stamp gate skips the read.
    assert backend_config.sync_process_env_from_env_file() is False

    env_file.write_text(
        "OPENBASE_CODING_BACKEND=claude_code\nOPENBASE_CODING_BACKENDS=claude_code,codex\n",
        encoding="utf-8",
    )
    assert backend_config.sync_process_env_from_env_file(force=True) is True
    assert backend_config.configured_execution_backends() == ["claude_code", "codex"]


def test_an_env_file_without_backend_keys_leaves_the_environment_alone(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("OPENBASE_CODING_BACKEND", "codex")
    _env_file(tmp_path, monkeypatch, "LIVEKIT_URL=ws://localhost:7880\n")

    assert backend_config.sync_process_env_from_env_file(force=True) is False
    assert backend_config.configured_execution_backends() == ["codex"]


def test_reset_session_manager_closes_the_previous_clients(monkeypatch) -> None:
    closed: list[str] = []

    class _Client:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            closed.append(self.name)

    manager = SimpleNamespace(
        _codex_clients=lambda: [_Client("shared"), _Client("dispatcher")]
    )
    monkeypatch.setattr(session_manager_module, "_session_manager", manager)

    async def run() -> None:
        session_manager_module.reset_session_manager()
        await asyncio.sleep(0)

    asyncio.run(run())
    assert session_manager_module._session_manager is None
    assert sorted(closed) == ["dispatcher", "shared"]


def test_voice_backend_client_is_built_per_job_unless_injected(monkeypatch) -> None:
    built: list[bool] = []
    monkeypatch.setattr(
        livekit,
        "_build_voice_backend_client",
        lambda *, persist_thread: built.append(persist_thread) or object(),
    )
    monkeypatch.setattr(livekit, "LIVEKIT_CODEX_FRESH_THREAD_PER_SESSION", False)
    monkeypatch.setattr(livekit, "_shared_voice_backend_client", None)

    first = livekit._voice_backend_client_for_job()
    second = livekit._voice_backend_client_for_job()
    assert first is not second
    assert built == [True, True]

    injected = object()
    monkeypatch.setattr(livekit, "_shared_voice_backend_client", injected)
    assert livekit._voice_backend_client_for_job() is injected
