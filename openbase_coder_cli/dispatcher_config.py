from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from openbase_coder_cli.backend_config import (
    CLAUDE_CODE_BACKEND,
    CODEX_BACKEND,
    CODING_BACKEND_ENV_KEY,
    OPENBASE_CLOUD_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
    normalize_backend,
)
from openbase_coder_cli.paths import CODEX_DISPATCHER_CONFIG_PATH, DEFAULT_ENV_FILE_PATH
from openbase_coder_cli.stt_providers import (
    DEFAULT_STT_PROVIDER_ID,
    LOCAL_MLX_WHISPER_STT_PROVIDER_ID,
    local_mlx_whisper_readiness,
    normalize_stt_provider_id,
)
from openbase_coder_cli.tts_providers import (
    CARTESIA_PROVIDER_ID,
    DEFAULT_CARTESIA_VOICE_ID,
    DEFAULT_TTS_PROVIDER_ID,
    KOKORO_PROVIDER_ID,
    OPENBASE_CLOUD_TTS_PROVIDER_ID,
    get_tts_provider,
    normalize_tts_provider_id,
)

# Bump alongside a forward-only migration; see the workspace AUTO_UPDATE.md.
SCHEMA_VERSION_KEY = "schema_version"
DISPATCHER_CONFIG_SCHEMA_VERSION = 1
REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}
DISPATCHER_REASONING_EFFORT_KEY = "dispatcher_reasoning_effort"
SUPER_AGENTS_REASONING_EFFORT_KEY = "super_agents_reasoning_effort"
DISPATCHER_SERVICE_TIER_KEY = "dispatcher_service_tier"
SUPER_AGENTS_SERVICE_TIER_KEY = "super_agents_service_tier"
SERVICE_TIERS = {"fast", "standard"}
# Fast dispatcher, standard super-agents by default: voice dispatch is
# latency-sensitive, bulk agent work defaults to the standard lane. Both
# scopes are settable in console settings (either can be fast).
DEFAULT_DISPATCHER_SERVICE_TIER = "fast"
DEFAULT_SUPER_AGENTS_SERVICE_TIER = "standard"
AUTO_LINK_PERSONAL_SKILLS_KEY = "auto_link_personal_skills"
BACKEND_MODELS_KEY = "backend_models"
DISPATCHER_MODEL_ROLE = "dispatcher"
SUPER_AGENTS_MODEL_ROLE = "super_agents"
CLAUDE_CODE_MODEL_OPTIONS = (
    {
        "id": "fable",
        "label": "Claude Fable 5",
        "description": "Claude Code family alias for claude-fable-5.",
    },
    {
        "id": "opus",
        "label": "Claude Opus",
        "description": "Claude Code family alias for the default Opus model.",
    },
    {
        "id": "sonnet",
        "label": "Claude Sonnet",
        "description": "Claude Code family alias for the default Sonnet model.",
    },
    {
        "id": "haiku",
        "label": "Claude Haiku",
        "description": "Claude Code family alias for the default Haiku model.",
    },
)
OPENBASE_CLOUD_CLAUDE_MODEL_OPTIONS = (
    {
        "id": "openbase-claude",
        "label": "Openbase Claude",
        "description": "Openbase Cloud Claude Code proxy model.",
    },
    {
        "id": "fable",
        "label": "Claude Fable 5",
        "description": "Claude Fable 5 through the Openbase Cloud proxy.",
    },
)
BACKEND_MODEL_OPTIONS = {
    CLAUDE_CODE_BACKEND: CLAUDE_CODE_MODEL_OPTIONS,
    OPENBASE_CLOUD_BACKEND: OPENBASE_CLOUD_CLAUDE_MODEL_OPTIONS,
}
CLAUDE_CODE_MODEL_ALIASES = {option["id"] for option in CLAUDE_CODE_MODEL_OPTIONS}
TTS_PROVIDER_KEY = "tts_provider"
STT_PROVIDER_KEY = "stt_provider"
DISPATCHER_VOICE_ID_KEY = "dispatcher_voice_id"
DISPATCHER_VOICE_NAME_KEY = "dispatcher_voice_name"
DEFAULT_DISPATCHER_VOICE_ID = DEFAULT_CARTESIA_VOICE_ID
DEFAULT_DISPATCHER_VOICE_NAME = "Jacqueline"


def read_dispatcher_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or CODEX_DISPATCHER_CONFIG_PATH
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    found_version = int(payload.get(SCHEMA_VERSION_KEY, 1) or 1)
    if found_version > DISPATCHER_CONFIG_SCHEMA_VERSION:
        raise ValueError(
            f"{config_path.name} schema {found_version} was written by a "
            "newer Openbase Coder; update the CLI."
        )
    return payload


def dispatcher_reasoning_effort(path: Path | None = None) -> str | None:
    value = _reasoning_effort_for_key(DISPATCHER_REASONING_EFFORT_KEY, path)
    return value if isinstance(value, str) and value in REASONING_EFFORTS else None


def super_agents_reasoning_effort(path: Path | None = None) -> str | None:
    value = _reasoning_effort_for_key(SUPER_AGENTS_REASONING_EFFORT_KEY, path)
    return value if isinstance(value, str) and value in REASONING_EFFORTS else None


def set_dispatcher_reasoning_effort(value: str, path: Path | None = None) -> Path:
    return _set_reasoning_effort(DISPATCHER_REASONING_EFFORT_KEY, value, path)


def set_super_agents_reasoning_effort(value: str, path: Path | None = None) -> Path:
    return _set_reasoning_effort(SUPER_AGENTS_REASONING_EFFORT_KEY, value, path)


def _service_tier_for_key(
    key: str, env_var: str, default: str, path: Path | None
) -> str:
    configured = _optional_str(read_dispatcher_config(path).get(key))
    if configured in SERVICE_TIERS:
        return configured
    env_value = _optional_str(os.getenv(env_var))
    if env_value in SERVICE_TIERS:
        return env_value
    env_file_value = _optional_str(_env_file_values(DEFAULT_ENV_FILE_PATH).get(env_var))
    if env_file_value in SERVICE_TIERS:
        return env_file_value
    return default


def dispatcher_service_tier(path: Path | None = None) -> str:
    return _service_tier_for_key(
        DISPATCHER_SERVICE_TIER_KEY,
        "DISPATCHER_SERVICE_TIER",
        DEFAULT_DISPATCHER_SERVICE_TIER,
        path,
    )


def super_agents_service_tier(path: Path | None = None) -> str:
    return _service_tier_for_key(
        SUPER_AGENTS_SERVICE_TIER_KEY,
        "SUPER_AGENTS_SERVICE_TIER",
        DEFAULT_SUPER_AGENTS_SERVICE_TIER,
        path,
    )


def _set_service_tier(key: str, value: str, path: Path | None) -> Path:
    normalized = value.strip().lower()
    if normalized not in SERVICE_TIERS:
        allowed = ", ".join(sorted(SERVICE_TIERS))
        raise ValueError(f"Service tier must be one of: {allowed}.")

    config_path = path or CODEX_DISPATCHER_CONFIG_PATH
    payload = {
        **read_dispatcher_config(config_path),
        key: normalized,
    }
    _write_dispatcher_config(payload, config_path)
    return config_path


def set_dispatcher_service_tier(value: str, path: Path | None = None) -> Path:
    return _set_service_tier(DISPATCHER_SERVICE_TIER_KEY, value, path)


def set_super_agents_service_tier(value: str, path: Path | None = None) -> Path:
    return _set_service_tier(SUPER_AGENTS_SERVICE_TIER_KEY, value, path)


def auto_link_personal_skills(path: Path | None = None) -> bool:
    return read_dispatcher_config(path).get(AUTO_LINK_PERSONAL_SKILLS_KEY) is True


def set_auto_link_personal_skills(enabled: bool, path: Path | None = None) -> Path:
    config_path = path or CODEX_DISPATCHER_CONFIG_PATH
    payload = {
        **read_dispatcher_config(config_path),
        AUTO_LINK_PERSONAL_SKILLS_KEY: bool(enabled),
    }
    _write_dispatcher_config(payload, config_path)
    return config_path


def super_agents_model(path: Path | None = None) -> str | None:
    return backend_model(SUPER_AGENTS_MODEL_ROLE, path=path)


def dispatcher_model(path: Path | None = None) -> str | None:
    return backend_model(DISPATCHER_MODEL_ROLE, path=path)


def backend_model(
    role: str,
    *,
    backend: str | None = None,
    path: Path | None = None,
) -> str | None:
    selected_backend = _execution_backend(
        _normalize_backend(backend or _configured_backend_from_environment())
    )
    configured_backend = _normalize_backend(
        backend or _configured_backend_from_environment()
    )
    payload = read_dispatcher_config(path)
    backend_models = payload.get(BACKEND_MODELS_KEY)
    if not isinstance(backend_models, dict):
        return None
    model_config = backend_models.get(configured_backend) or backend_models.get(
        selected_backend
    )
    if not isinstance(model_config, dict):
        return None
    configured = _optional_str(model_config.get(role))
    if configured:
        return configured
    return None


def set_super_agents_model(value: str, path: Path | None = None) -> Path:
    return set_backend_model(SUPER_AGENTS_MODEL_ROLE, value, path=path)


def set_dispatcher_model(value: str, path: Path | None = None) -> Path:
    return set_backend_model(DISPATCHER_MODEL_ROLE, value, path=path)


def set_backend_model(
    role: str,
    value: str,
    *,
    backend: str | None = None,
    path: Path | None = None,
) -> Path:
    if role not in {DISPATCHER_MODEL_ROLE, SUPER_AGENTS_MODEL_ROLE}:
        raise ValueError("Model role must be dispatcher or super_agents.")
    normalized = " ".join(value.split())
    if not normalized:
        raise ValueError("Model cannot be blank.")
    selected_backend = _normalize_backend(
        backend or _configured_backend_from_environment()
    )
    config_path = path or CODEX_DISPATCHER_CONFIG_PATH
    payload = read_dispatcher_config(config_path)
    backend_models = payload.get(BACKEND_MODELS_KEY)
    if not isinstance(backend_models, dict):
        backend_models = {}
    model_config = backend_models.get(selected_backend)
    if not isinstance(model_config, dict):
        model_config = {}
    backend_models[selected_backend] = {**model_config, role: normalized}
    _write_dispatcher_config(
        {
            **payload,
            BACKEND_MODELS_KEY: backend_models,
        },
        config_path,
    )
    return config_path


def model_options_for_backend(backend: str | None = None) -> tuple[dict[str, str], ...]:
    selected_backend = _normalize_backend(
        backend or _configured_backend_from_environment()
    )
    return tuple(BACKEND_MODEL_OPTIONS.get(selected_backend, ()))


def is_known_backend_model(model: str, *, backend: str | None = None) -> bool:
    normalized = " ".join(model.split()).lower()
    return any(
        normalized == option["id"].lower()
        for option in model_options_for_backend(backend)
    )


def selected_tts_provider_id(path: Path | None = None) -> str:
    payload = read_dispatcher_config(path)
    configured = _optional_str(payload.get(TTS_PROVIDER_KEY))
    if configured:
        try:
            return normalize_tts_provider_id(configured)
        except ValueError:
            return DEFAULT_TTS_PROVIDER_ID
    return DEFAULT_TTS_PROVIDER_ID


def selected_stt_provider_id(path: Path | None = None) -> str:
    payload = read_dispatcher_config(path)
    configured = _optional_str(payload.get(STT_PROVIDER_KEY))
    if configured:
        try:
            return normalize_stt_provider_id(configured)
        except ValueError:
            return DEFAULT_STT_PROVIDER_ID
    env_provider = _optional_str(os.getenv("LIVEKIT_STT_PROVIDER"))
    if env_provider:
        try:
            return normalize_stt_provider_id(env_provider)
        except ValueError:
            return DEFAULT_STT_PROVIDER_ID
    return DEFAULT_STT_PROVIDER_ID


def set_stt_provider(provider_id: str, path: Path | None = None) -> dict[str, str]:
    normalized_provider_id = normalize_stt_provider_id(provider_id)
    if (
        normalized_provider_id == LOCAL_MLX_WHISPER_STT_PROVIDER_ID
        and not local_mlx_whisper_readiness().ready
    ):
        raise ValueError("Download local MLX Whisper before selecting local STT.")

    config_path = path or CODEX_DISPATCHER_CONFIG_PATH
    _write_dispatcher_config(
        {
            **read_dispatcher_config(config_path),
            STT_PROVIDER_KEY: normalized_provider_id,
        },
        config_path,
    )
    return {"provider": normalized_provider_id}


def dispatcher_voice(path: Path | None = None) -> dict[str, str]:
    payload = read_dispatcher_config(path)
    provider_id = selected_tts_provider_id(path)
    provider = get_tts_provider(provider_id)
    default_voice = provider.default_dispatcher_voice()
    legacy_env_voice_id = os.getenv("CARTESIA_VOICE_ID", "").strip()
    configured_voice_id = _optional_str(payload.get(DISPATCHER_VOICE_ID_KEY))
    voice_id = (
        configured_voice_id
        or (
            legacy_env_voice_id
            if provider_id in {CARTESIA_PROVIDER_ID, OPENBASE_CLOUD_TTS_PROVIDER_ID}
            else ""
        )
        or default_voice.id
    )
    catalog_voice = provider.voice_for_id(voice_id)
    configured_voice_name = _optional_str(payload.get(DISPATCHER_VOICE_NAME_KEY))
    if catalog_voice is None and configured_voice_id and configured_voice_name:
        return {
            "id": configured_voice_id,
            "name": configured_voice_name,
            "provider": provider_id,
        }
    if catalog_voice is None and provider_id in {
        CARTESIA_PROVIDER_ID,
        OPENBASE_CLOUD_TTS_PROVIDER_ID,
    }:
        voice_id = DEFAULT_DISPATCHER_VOICE_ID
        catalog_voice = provider.voice_for_id(voice_id)
    if catalog_voice is None:
        voice_id = default_voice.id
        catalog_voice = default_voice
    voice_name = (
        catalog_voice.name or configured_voice_name or DEFAULT_DISPATCHER_VOICE_NAME
    )
    return {
        "id": voice_id,
        "name": voice_name,
        "provider": provider_id,
    }


def set_tts_provider_and_dispatcher_voice(
    *,
    provider_id: str,
    voice_id: str,
    path: Path | None = None,
) -> dict[str, str]:
    normalized_provider_id = normalize_tts_provider_id(provider_id)
    provider = get_tts_provider(normalized_provider_id)
    if normalized_provider_id == KOKORO_PROVIDER_ID and not provider.readiness().ready:
        raise ValueError("Download Kokoro local voices before selecting Kokoro.")
    normalized = voice_id.strip()
    voice = provider.voice_for_id(normalized)
    if voice is None:
        raise ValueError("Dispatcher voice must be selected from the provider catalog.")

    config_path = path or CODEX_DISPATCHER_CONFIG_PATH
    _write_dispatcher_config(
        {
            **read_dispatcher_config(config_path),
            TTS_PROVIDER_KEY: normalized_provider_id,
            DISPATCHER_VOICE_ID_KEY: voice.id,
            DISPATCHER_VOICE_NAME_KEY: voice.name,
        },
        config_path,
    )
    return {"id": voice.id, "name": voice.name, "provider": normalized_provider_id}


def set_dispatcher_voice(voice_id: str, path: Path | None = None) -> dict[str, str]:
    return set_tts_provider_and_dispatcher_voice(
        provider_id=selected_tts_provider_id(path),
        voice_id=voice_id,
        path=path,
    )


def _reasoning_effort_for_key(key: str, path: Path | None = None) -> str | None:
    value = read_dispatcher_config(path).get(key)
    return value if isinstance(value, str) else None


def _set_reasoning_effort(key: str, value: str, path: Path | None = None) -> Path:
    if value not in REASONING_EFFORTS:
        allowed = ", ".join(sorted(REASONING_EFFORTS))
        raise ValueError(f"Reasoning effort must be one of: {allowed}.")

    config_path = path or CODEX_DISPATCHER_CONFIG_PATH
    payload = {**read_dispatcher_config(config_path), key: value}
    _write_dispatcher_config(payload, config_path)
    return config_path


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _configured_backend_from_environment() -> str:
    return (
        os.getenv(CODING_BACKEND_ENV_KEY)
        or _env_file_values(DEFAULT_ENV_FILE_PATH).get(CODING_BACKEND_ENV_KEY)
        or CODEX_BACKEND
    )


def _normalize_backend(value: str | None) -> str:
    try:
        return normalize_backend(value)
    except ValueError:
        return CODEX_BACKEND


def _execution_backend(backend: str) -> str:
    if backend == OPENBASE_CLOUD_BACKEND:
        return CLAUDE_CODE_BACKEND
    if backend == OPENBASE_CLOUD_CODEX_BACKEND:
        return CODEX_BACKEND
    return backend


def _env_file_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _write_dispatcher_config(payload: dict[str, Any], config_path: Path) -> None:
    payload = {**payload, SCHEMA_VERSION_KEY: DISPATCHER_CONFIG_SCHEMA_VERSION}
    config_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=config_path.parent, delete=False
    ) as tmp:
        json.dump(payload, tmp, indent=2, sort_keys=True)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, config_path)
