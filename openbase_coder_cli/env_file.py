"""Shared helpers for reading and updating the Openbase Coder .env file."""

from __future__ import annotations

import shlex
from pathlib import Path

from openbase_coder_cli.backend_config import (
    CODING_BACKEND_ENV_KEY,
    DEFAULT_CODING_BACKEND,
    normalize_backend,
)


def env_file_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        key = active_env_key(line)
        if key is None:
            continue
        _raw_key, raw_value = line.split("=", 1)
        values[key] = parse_env_value(raw_value.strip())
    return values


def upsert_env_file_values(path: Path, values: dict[str, str]) -> None:
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    remaining = dict(values)
    updated: list[str] = []
    for line in lines:
        key = active_env_key(line)
        if key in remaining:
            updated.append(f"{key}={format_env_value(remaining.pop(key))}")
        else:
            updated.append(line)
    if updated and updated[-1].strip():
        updated.append("")
    for key, value in remaining.items():
        updated.append(f"{key}={format_env_value(value)}")
    path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")


def remove_env_file_keys(path: Path, keys: set[str]) -> bool:
    """Remove active assignments for ``keys`` while preserving the rest."""
    if not path.is_file():
        return False
    lines = path.read_text(encoding="utf-8").splitlines()
    filtered = [line for line in lines if active_env_key(line) not in keys]
    if filtered == lines:
        return False
    path.write_text("\n".join(filtered).rstrip() + "\n", encoding="utf-8")
    return True


def selected_backend_from_env_file(path: Path) -> str:
    """The coding backend selected by an env file."""
    values = env_file_values(path)
    raw_value = values.get(CODING_BACKEND_ENV_KEY)
    try:
        return normalize_backend(raw_value)
    except ValueError:
        return DEFAULT_CODING_BACKEND


def parse_env_value(value: str) -> str:
    try:
        parts = shlex.split(value, comments=False, posix=True)
    except ValueError:
        return value
    return parts[0] if len(parts) == 1 else value


def active_env_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    key, _value = stripped.split("=", 1)
    key = key.strip()
    return key if key else None


def format_env_value(value: str) -> str:
    if (
        not value
        or any(char.isspace() for char in value)
        or any(char in value for char in ['"', "'", "#"])
    ):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value
