"""Runtime .env settings API views."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from rest_framework import serializers, status
from rest_framework.decorators import api_view
from rest_framework.response import Response

from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
COMMON_ENV_KEYS = (
    "OPENBASE_CODING_BACKEND",
    "SUPER_AGENTS_DEFAULT_BACKEND",
    "OPENBASE_CODER_CLI_WEB_BACKEND_URL",
    "OPENBASE_CLOUD_AUDIO_BASE_URL",
    "OPENBASE_CLOUD_AUDIO_CARTESIA_VERSION",
    "ASSEMBLY_AI_API_KEY",
    "CARTESIA_API_KEY",
    "DEEPGRAM_API_KEY",
    "CODEX_MODEL",
    "CODEX_MODEL_REASONING_EFFORT",
    "CODEX_SERVICE_TIER",
    "SUPER_AGENTS_CLAUDE_PERMISSION_MODE",
    "CODEX_APP_SERVER_URL",
    "LIVEKIT_URL",
    "LIVEKIT_CODEX_THREAD_CWD",
    "LIVEKIT_NETWORK_MODE",
    "OPENBASE_CODER_CLI_ALLOWED_HOSTS",
)
SECRET_KEY_PARTS = ("KEY", "SECRET", "TOKEN", "PASSWORD")


class EnvEntrySerializer(serializers.Serializer):
    key = serializers.CharField(trim_whitespace=True, max_length=128)
    value = serializers.CharField(
        allow_blank=True,
        trim_whitespace=False,
        max_length=8192,
    )

    def validate_key(self, value: str) -> str:
        key = value.strip().upper()
        if not ENV_KEY_PATTERN.match(key):
            raise serializers.ValidationError(
                "Environment variable names must use uppercase letters, numbers, and underscores."
            )
        return key


class EnvSettingsSerializer(serializers.Serializer):
    entries = EnvEntrySerializer(many=True)
    deleted_keys = serializers.ListField(
        child=serializers.CharField(trim_whitespace=True, max_length=128),
        required=False,
    )

    def validate(self, attrs):
        keys = [entry["key"] for entry in attrs["entries"]]
        if len(keys) != len(set(keys)):
            raise serializers.ValidationError(
                "Environment variable names must be unique."
            )
        deleted_keys = []
        for raw_key in attrs.get("deleted_keys", []):
            key = raw_key.strip().upper()
            if key and not ENV_KEY_PATTERN.match(key):
                raise serializers.ValidationError("Deleted environment key is invalid.")
            if key:
                deleted_keys.append(key)
        attrs["deleted_keys"] = deleted_keys
        return attrs


@api_view(["GET", "PUT"])
def env_settings(request):
    """Read or update active variables in the runtime .env file."""
    if request.method == "GET":
        return Response(_env_payload(), status=status.HTTP_200_OK)

    serializer = EnvSettingsSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    entries = {
        entry["key"]: entry["value"] for entry in serializer.validated_data["entries"]
    }
    _write_env_entries(
        DEFAULT_ENV_FILE_PATH,
        entries,
        set(serializer.validated_data.get("deleted_keys", [])),
    )
    return Response(
        {
            **_env_payload(),
            "changed": True,
            "restart_required": True,
            "restart_hint": "Restart Openbase services for environment changes to apply.",
        },
        status=status.HTTP_200_OK,
    )


def _env_payload() -> dict:
    values = _read_env_values(DEFAULT_ENV_FILE_PATH)
    return {
        "env_file_exists": DEFAULT_ENV_FILE_PATH.is_file(),
        "entries": [
            {"key": key, "value": value, "secret": _is_secret_key(key)}
            for key, value in values.items()
        ],
        "common_keys": list(COMMON_ENV_KEYS),
        "changed": False,
        "restart_required": False,
        "restart_hint": "Restart Openbase services for environment changes to apply.",
    }


def _read_env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        key = _active_env_key(line)
        if key is None:
            continue
        _raw_key, raw_value = line.split("=", 1)
        values[key] = _parse_env_value(raw_value.strip())
    return values


def _write_env_entries(
    path: Path,
    entries: dict[str, str],
    deleted_keys: set[str],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    pending = dict(entries)
    updated: list[str] = []

    for line in lines:
        key = _active_env_key(line)
        if key is None:
            updated.append(line)
            continue
        if key in deleted_keys:
            pending.pop(key, None)
            continue
        if key in pending:
            updated.append(f"{key}={_format_env_value(pending.pop(key, ''))}")
        else:
            updated.append(line)

    if pending:
        if updated and updated[-1].strip():
            updated.append("")
        for key in sorted(pending):
            updated.append(f"{key}={_format_env_value(pending[key])}")

    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


def _active_env_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _value = stripped.split("=", 1)
    key = key.strip()
    return key if ENV_KEY_PATTERN.match(key) else None


def _parse_env_value(value: str) -> str:
    try:
        parts = shlex.split(value, comments=False, posix=True)
    except ValueError:
        return value
    return parts[0] if len(parts) == 1 else value


def _format_env_value(value: str) -> str:
    if (
        not value
        or any(char.isspace() for char in value)
        or any(char in value for char in ['"', "'", "#"])
    ):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _is_secret_key(key: str) -> bool:
    return any(part in key for part in SECRET_KEY_PARTS)
