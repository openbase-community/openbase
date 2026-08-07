from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from openbase_coder_cli.backend_config import (
    CODEX_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
from openbase_coder_cli.paths import CODEX_HOME_DIR

CODEX_CONFIG_NAME = "config.toml"
OPENBASE_CLOUD_PROVIDER = "openbase_cloud"
OPENBASE_CLOUD_PROVIDER_TABLE = f"model_providers.{OPENBASE_CLOUD_PROVIDER}"
DEFAULT_CODEX_MODEL = "gpt-5.5"
DEFAULT_OPENBASE_CLOUD_CODEX_MODEL = "openbase-codex"
DEFAULT_OPENBASE_CLOUD_BASE_URL = "https://app.openbase.cloud"
OPENBASE_CLOUD_LLM_PATH = "/api/openbase/llm/openai/v1"


@dataclass(frozen=True)
class CodexBackendConfigResult:
    path: Path
    changed: bool


def codex_config_path_for_env_file(env_file: Path) -> Path:
    return env_file.parent / "codex_home" / CODEX_CONFIG_NAME


def apply_backend_to_codex_config(
    backend: str,
    *,
    config_path: Path | None = None,
    web_backend_url: str | None = None,
) -> CodexBackendConfigResult:
    path = config_path or CODEX_HOME_DIR / CODEX_CONFIG_NAME
    existing = path.read_text(encoding="utf-8") if path.is_file() else ""

    if backend == OPENBASE_CLOUD_CODEX_BACKEND:
        updated = _apply_openbase_cloud_config(existing, web_backend_url)
    elif backend == CODEX_BACKEND:
        updated = _apply_direct_codex_config(existing)
    else:
        return CodexBackendConfigResult(path=path, changed=False)

    if updated == existing:
        return CodexBackendConfigResult(path=path, changed=False)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(updated, encoding="utf-8")
    return CodexBackendConfigResult(path=path, changed=True)


def _apply_openbase_cloud_config(text: str, web_backend_url: str | None) -> str:
    base_url = _openbase_cloud_llm_base_url(web_backend_url)
    model = os.getenv("OPENBASE_CLOUD_CODEX_MODEL", DEFAULT_OPENBASE_CLOUD_CODEX_MODEL)
    updated = _remove_toml_root_keys(text, {"model", "model_provider"})
    updated = _ensure_toml_root_values(
        updated,
        (
            ("model", json.dumps(model)),
            ("model_provider", json.dumps(OPENBASE_CLOUD_PROVIDER)),
        ),
    )
    block = (
        f"[{OPENBASE_CLOUD_PROVIDER_TABLE}]\n"
        'name = "Openbase Cloud"\n'
        f"base_url = {json.dumps(base_url)}\n"
        'env_key = "OPENBASE_CLOUD_CODEX_API_KEY"\n'
        'wire_api = "responses"\n'
    )
    return _replace_toml_table(updated, OPENBASE_CLOUD_PROVIDER_TABLE, block)


def _apply_direct_codex_config(text: str) -> str:
    model = os.getenv("CODEX_MODEL", DEFAULT_CODEX_MODEL)
    updated = _remove_toml_root_keys(text, {"model", "model_provider"})
    updated = _remove_toml_table(updated, OPENBASE_CLOUD_PROVIDER_TABLE)
    return _ensure_toml_root_values(updated, (("model", json.dumps(model)),))


def _openbase_cloud_llm_base_url(web_backend_url: str | None) -> str:
    configured = (
        os.getenv("OPENBASE_CLOUD_LLM_BASE_URL")
        or web_backend_url
        or os.getenv("OPENBASE_CODER_CLI_WEB_BACKEND_URL")
        or DEFAULT_OPENBASE_CLOUD_BASE_URL
    )
    configured = configured.rstrip("/")
    if configured.endswith("/api/openbase/llm/openai/v1"):
        return configured
    return f"{configured}{OPENBASE_CLOUD_LLM_PATH}"


def _ensure_toml_root_values(
    text: str,
    values: tuple[tuple[str, str], ...],
) -> str:
    lines = text.splitlines()
    first_table_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("[") and line.strip().endswith("]")
        ),
        len(lines),
    )
    root_lines = lines[:first_table_index]
    table_lines = lines[first_table_index:]
    keys = {key for key, _value in values}
    updated_root = [line for line in root_lines if _toml_root_key(line) not in keys]

    while updated_root and not updated_root[-1].strip():
        updated_root.pop()

    for key, value in values:
        updated_root.append(f"{key} = {value}")

    while table_lines and not table_lines[0].strip():
        table_lines.pop(0)

    if table_lines:
        return "\n".join(updated_root) + "\n\n" + "\n".join(table_lines) + "\n"
    return "\n".join(updated_root) + "\n"


def _remove_toml_root_keys(text: str, keys: set[str]) -> str:
    lines = text.splitlines()
    first_table_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.strip().startswith("[") and line.strip().endswith("]")
        ),
        len(lines),
    )
    root_lines = [
        line for line in lines[:first_table_index] if _toml_root_key(line) not in keys
    ]
    table_lines = lines[first_table_index:]

    while root_lines and not root_lines[-1].strip():
        root_lines.pop()
    while table_lines and not table_lines[0].strip():
        table_lines.pop(0)

    if root_lines and table_lines:
        return "\n".join(root_lines) + "\n\n" + "\n".join(table_lines) + "\n"
    if root_lines:
        return "\n".join(root_lines) + "\n"
    if table_lines:
        return "\n".join(table_lines) + "\n"
    return ""


def _toml_root_key(line: str) -> str | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in stripped:
        return None
    return stripped.split("=", 1)[0].strip()


def _replace_toml_table(text: str, table_name: str, block: str) -> str:
    updated = _remove_toml_table(text, table_name)
    updated = updated.rstrip()
    if updated:
        return f"{updated}\n\n{block}"
    return block


def _remove_toml_table(text: str, table_name: str) -> str:
    target_header = f"[{table_name}]"
    lines = text.splitlines()
    output: list[str] = []
    index = 0

    while index < len(lines):
        if lines[index].strip() == target_header:
            index += 1
            while index < len(lines):
                stripped = lines[index].strip()
                if stripped.startswith("[") and stripped.endswith("]"):
                    break
                index += 1
            while output and not output[-1].strip():
                output.pop()
            continue

        output.append(lines[index])
        index += 1

    while output and not output[-1].strip():
        output.pop()

    return "\n".join(output) + ("\n" if output else "")
