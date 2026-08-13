"""Dispatcher phase: dispatcher config plus voice audio providers and models."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import click

from openbase_coder_cli.dispatcher_config import (
    DISPATCHER_VOICE_ID_KEY,
    DISPATCHER_VOICE_NAME_KEY,
    STT_PROVIDER_KEY,
    TTS_PROVIDER_KEY,
)
from openbase_coder_cli.local_audio import LOCAL_AUDIO_PYTHON_MAX
from openbase_coder_cli.paths import (
    CODEX_DISPATCHER_CONFIG_PATH,
)
from openbase_coder_cli.stt_providers import (
    ASSEMBLYAI_STT_PROVIDER_ID,
    LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
    OPENBASE_CLOUD_STT_PROVIDER_ID,
    download_local_mlx_whisper,
)
from openbase_coder_cli.tts_providers import (
    CARTESIA_PROVIDER_ID,
    KOKORO_PROVIDER_ID,
    OPENBASE_CLOUD_TTS_PROVIDER_ID,
    get_tts_provider,
)

CODEX_HOME_DEFAULT_DISPATCHER_CONFIG = {
    "dispatcher_reasoning_effort": "low",
    "super_agents_reasoning_effort": "high",
    "backend_models": {
        "codex": {"dispatcher": "gpt-5.5", "super_agents": "gpt-5.5"},
        "claude_code": {"dispatcher": "opus", "super_agents": "opus"},
    },
}
AUDIO_PROVIDER_OPENBASE_CLOUD = "openbase-cloud"
AUDIO_PROVIDER_CARTESIA = "cartesia"
AUDIO_PROVIDER_LOCAL = "local"
AUDIO_PROVIDER_OPTIONS = (
    AUDIO_PROVIDER_OPENBASE_CLOUD,
    AUDIO_PROVIDER_CARTESIA,
    AUDIO_PROVIDER_LOCAL,
)
DEFAULT_AUDIO_PROVIDER = AUDIO_PROVIDER_OPENBASE_CLOUD
LOCAL_AUDIO_REQUIREMENTS = (
    "huggingface-hub>=0.36.0",
    "kokoro>=0.9.4",
    "mlx-whisper>=0.4.3",
)


def _ensure_codex_home_dispatcher_config(audio_provider: str | None = None) -> None:
    """Create the missing Openbase dispatcher config."""
    if CODEX_DISPATCHER_CONFIG_PATH.exists():
        if audio_provider:
            _update_dispatcher_audio_provider(
                CODEX_DISPATCHER_CONFIG_PATH,
                audio_provider,
            )
        click.echo(
            f"Openbase dispatcher config already exists at "
            f"{CODEX_DISPATCHER_CONFIG_PATH}"
        )
        return

    CODEX_DISPATCHER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CODEX_DISPATCHER_CONFIG_PATH.write_text(
        json.dumps(
            _default_dispatcher_config(audio_provider or DEFAULT_AUDIO_PROVIDER),
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    click.echo(f"Created Openbase dispatcher config at {CODEX_DISPATCHER_CONFIG_PATH}")


def _default_dispatcher_config(audio_provider: str) -> dict[str, object]:
    return {
        **CODEX_HOME_DEFAULT_DISPATCHER_CONFIG,
        **_audio_provider_config(audio_provider),
    }


def _update_dispatcher_audio_provider(path: Path, audio_provider: str) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.update(_audio_provider_config(audio_provider))
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    click.echo(f"Updated voice audio provider in {path}.")


def _audio_provider_config(audio_provider: str) -> dict[str, str]:
    if audio_provider == AUDIO_PROVIDER_OPENBASE_CLOUD:
        tts_provider = OPENBASE_CLOUD_TTS_PROVIDER_ID
        stt_provider = OPENBASE_CLOUD_STT_PROVIDER_ID
    elif audio_provider == AUDIO_PROVIDER_CARTESIA:
        tts_provider = CARTESIA_PROVIDER_ID
        stt_provider = ASSEMBLYAI_STT_PROVIDER_ID
    elif audio_provider == AUDIO_PROVIDER_LOCAL:
        tts_provider = KOKORO_PROVIDER_ID
        stt_provider = LOCAL_MLX_WHISPER_STT_PROVIDER_ID
    else:
        raise click.ClickException(f"Unsupported audio provider: {audio_provider}")

    voice = get_tts_provider(tts_provider).default_dispatcher_voice()
    return {
        TTS_PROVIDER_KEY: tts_provider,
        STT_PROVIDER_KEY: stt_provider,
        DISPATCHER_VOICE_ID_KEY: voice.id,
        DISPATCHER_VOICE_NAME_KEY: voice.name,
    }


def _download_local_audio_models() -> None:
    click.echo("Downloading local TTS voices...")
    tts_status = get_tts_provider(KOKORO_PROVIDER_ID).download_all_voices()
    if not tts_status.ready:
        raise click.ClickException(
            tts_status.detail or "Unable to download local TTS voices."
        )
    click.echo("Downloading local STT model...")
    stt_status = download_local_mlx_whisper()
    if not stt_status.ready:
        raise click.ClickException(
            stt_status.detail or "Unable to download local STT model."
        )
    click.echo("Downloaded local voice audio models.")


def _ensure_local_audio_dependencies(runtime_package) -> None:
    python_path = (
        runtime_package.python_path if runtime_package else Path(sys.executable)
    )
    version = _python_version(python_path)
    if version >= LOCAL_AUDIO_PYTHON_MAX:
        raise click.ClickException(
            "Local audio currently requires a Python 3.12 Openbase Coder runtime "
            "because Kokoro declares Python <3.13. Reinstall Openbase Coder with "
            "a Python 3.12 standalone package, or use --audio-provider openbase-cloud."
        )
    if _local_audio_dependencies_available(python_path):
        return

    click.echo("Installing local audio Python dependencies...")
    try:
        subprocess.run(
            [
                str(python_path),
                "-m",
                "pip",
                "install",
                "--upgrade",
                *LOCAL_AUDIO_REQUIREMENTS,
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(
            "Failed to install local audio dependencies. "
            "Use --audio-provider openbase-cloud or retry after checking network access."
        ) from exc


def _local_audio_dependencies_available(python_path: Path) -> bool:
    result = subprocess.run(
        [
            str(python_path),
            "-c",
            "import huggingface_hub, kokoro, mlx_whisper",
        ],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _python_version(python_path: Path) -> tuple[int, int]:
    result = subprocess.run(
        [
            str(python_path),
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    major, minor = result.stdout.strip().split(".", 1)
    return int(major), int(minor)
