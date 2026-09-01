"""Backend-dependent Codex app-server launch overrides.

The service app-server runs against the shared ``~/.codex`` home, so backend
model/provider choices are passed as ``-c`` launch overrides scoped to the
service process — never written into the user's config.toml.
"""

from __future__ import annotations

import json
import os

from openbase_coder_cli.backend_config import (
    CODEX_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)

OPENBASE_CLOUD_PROVIDER = "openbase_cloud"
DEFAULT_CODEX_MODEL = "gpt-5.5"
# Real public model id on the Cloud OpenAI proxy; the legacy "openbase-codex"
# alias is still accepted server-side for older installs.
DEFAULT_OPENBASE_CLOUD_CODEX_MODEL = "gpt-5.5"
DEFAULT_OPENBASE_CLOUD_BASE_URL = "https://app.openbase.cloud"
OPENBASE_CLOUD_LLM_PATH = "/api/openbase/llm/openai/v1"


def codex_backend_cli_overrides(
    backend: str,
    *,
    web_backend_url: str | None = None,
) -> list[str]:
    """``codex app-server`` ``-c`` arguments for the selected backend."""
    if backend == OPENBASE_CLOUD_CODEX_BACKEND:
        base_url = _openbase_cloud_llm_base_url(web_backend_url)
        model = os.getenv(
            "OPENBASE_CLOUD_CODEX_MODEL", DEFAULT_OPENBASE_CLOUD_CODEX_MODEL
        )
        provider = f"model_providers.{OPENBASE_CLOUD_PROVIDER}"
        return _config_args(
            ("model", model),
            ("model_provider", OPENBASE_CLOUD_PROVIDER),
            (f"{provider}.name", "Openbase Cloud"),
            (f"{provider}.base_url", base_url),
            (f"{provider}.env_key", "OPENBASE_CLOUD_CODEX_API_KEY"),
            (f"{provider}.wire_api", "responses"),
        )
    if backend == CODEX_BACKEND:
        model = os.getenv("CODEX_MODEL", DEFAULT_CODEX_MODEL)
        return _config_args(("model", model))
    return []


def _config_args(*values: tuple[str, str]) -> list[str]:
    args: list[str] = []
    for key, value in values:
        # TOML basic strings share JSON's escaping rules.
        args.extend(["-c", f"{key}={json.dumps(value)}"])
    return args


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
