from __future__ import annotations

import json
from pathlib import Path

from openbase_coder_cli.paths import CONSOLE_SETTINGS_JSON_PATH

DEFAULT_DANGEROUS_CONFIRMATION_PHRASE = "yes, proceed"
DEFAULT_USER_ADDRESS_NAME = "there"
DEFAULT_KEEP_SYSTEM_AWAKE = False


def get_ignored_launchctl_labels() -> list[str]:
    data = _read_settings()
    labels = data.get("ignored_launchctl_labels")
    if not isinstance(labels, list):
        return []
    return sorted(
        {label for label in labels if isinstance(label, str) and label.strip()}
    )


def set_ignored_launchctl_labels(labels: list[str]) -> list[str]:
    normalized = sorted(
        {label.strip() for label in labels if isinstance(label, str) and label.strip()}
    )
    data = _read_settings()
    data["ignored_launchctl_labels"] = normalized
    _write_settings(data)
    return normalized


def get_dangerous_confirmation_phrase() -> str:
    data = _read_settings()
    phrase = data.get("dangerous_confirmation_phrase")
    if not isinstance(phrase, str) or not phrase.strip():
        return DEFAULT_DANGEROUS_CONFIRMATION_PHRASE
    return phrase.strip()


def set_dangerous_confirmation_phrase(phrase: str) -> str:
    normalized = phrase.strip()
    if not normalized:
        raise ValueError("Dangerous confirmation phrase cannot be blank.")
    data = _read_settings()
    data["dangerous_confirmation_phrase"] = normalized
    _write_settings(data)
    return normalized


def get_user_address_name() -> str:
    data = _read_settings()
    name = data.get("user_address_name")
    if not isinstance(name, str) or not name.strip():
        return DEFAULT_USER_ADDRESS_NAME
    return name.strip()


def set_user_address_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise ValueError("User address name cannot be blank.")
    data = _read_settings()
    data["user_address_name"] = normalized
    _write_settings(data)
    return normalized


def get_keep_system_awake_enabled() -> bool:
    data = _read_settings()
    value = data.get("keep_system_awake")
    if isinstance(value, bool):
        return value
    return DEFAULT_KEEP_SYSTEM_AWAKE


def set_keep_system_awake_enabled(value: bool) -> bool:
    data = _read_settings()
    data["keep_system_awake"] = bool(value)
    _write_settings(data)
    return bool(value)


def _read_settings() -> dict:
    try:
        data = json.loads(CONSOLE_SETTINGS_JSON_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def _write_settings(data: dict) -> None:
    CONSOLE_SETTINGS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = Path(f"{CONSOLE_SETTINGS_JSON_PATH}.tmp")
    tmp_path.write_text(
        json.dumps(data, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    tmp_path.replace(CONSOLE_SETTINGS_JSON_PATH)
