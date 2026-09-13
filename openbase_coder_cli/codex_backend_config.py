"""Backend-dependent defaults for session-scoped Codex profiles."""

from __future__ import annotations

import os
from typing import Any

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


def codex_backend_profile_config(
    backend: str,
    *,
    web_backend_url: str | None = None,
) -> dict[str, Any]:
    """Provider routing belongs to a profile, never shared daemon arguments."""
    if backend == OPENBASE_CLOUD_CODEX_BACKEND:
        base_url = _openbase_cloud_llm_base_url(web_backend_url)
        model = os.getenv(
            "OPENBASE_CLOUD_CODEX_MODEL", DEFAULT_OPENBASE_CLOUD_CODEX_MODEL
        )
        return {
            "model": model,
            "model_provider": OPENBASE_CLOUD_PROVIDER,
            "model_providers": {
                OPENBASE_CLOUD_PROVIDER: {
                    "name": "Openbase Cloud",
                    "base_url": base_url,
                    "env_key": "OPENBASE_CLOUD_CODEX_API_KEY",
                    "wire_api": "responses",
                },
            },
        }
    if backend == CODEX_BACKEND:
        return {"model": DEFAULT_CODEX_MODEL, "model_provider": "openai"}
    return {}


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
