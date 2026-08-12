from __future__ import annotations

from collections.abc import Callable

CODEX_BACKEND = "codex"
OPENBASE_CLOUD_BACKEND = "openbase_cloud"
OPENBASE_CLOUD_CODEX_BACKEND = "openbase_cloud_codex"
CLAUDE_CODE_BACKEND = "claude_code"
SELECTABLE_BACKENDS = (CODEX_BACKEND, OPENBASE_CLOUD_BACKEND, CLAUDE_CODE_BACKEND)
SUPPORTED_BACKENDS = (
    CODEX_BACKEND,
    OPENBASE_CLOUD_BACKEND,
    CLAUDE_CODE_BACKEND,
    OPENBASE_CLOUD_CODEX_BACKEND,
)
DEFAULT_CODING_BACKEND = "codex"
CODING_BACKEND_ENV_KEY = "OPENBASE_CODING_BACKEND"
BACKEND_ALIASES = {
    "codex": CODEX_BACKEND,
    "codecs": CODEX_BACKEND,
    "openbase cloud": OPENBASE_CLOUD_BACKEND,
    "claude code": CLAUDE_CODE_BACKEND,
    "cloud code": CLAUDE_CODE_BACKEND,
    "openbase cloud codex": OPENBASE_CLOUD_CODEX_BACKEND,
    "openbase cloud codecs": OPENBASE_CLOUD_CODEX_BACKEND,
    "codex via openbase cloud": OPENBASE_CLOUD_CODEX_BACKEND,
}


def normalize_backend(value: str | None) -> str:
    raw = _normalize_backend_alias(value)
    if not raw:
        return DEFAULT_CODING_BACKEND
    aliases = {
        _normalize_backend_alias(alias): backend
        for alias, backend in BACKEND_ALIASES.items()
    }
    try:
        return aliases[raw]
    except KeyError as exc:
        supported = ", ".join(SELECTABLE_BACKENDS)
        raise ValueError(
            f"Unsupported backend: {value}. Supported backends: {supported}."
        ) from exc


def _normalize_backend_alias(value: str | None) -> str:
    raw = (value or "").strip().lower()
    return " ".join(raw.replace("_", " ").replace("-", " ").split())


def execution_backend_for_configured_backend(backend: str) -> str:
    """Map a configured backend to the backend that actually executes turns."""
    if backend == OPENBASE_CLOUD_BACKEND:
        return CLAUDE_CODE_BACKEND
    if backend == OPENBASE_CLOUD_CODEX_BACKEND:
        return CODEX_BACKEND
    return backend


def configured_execution_backend(
    environment_backend: Callable[[], str] | None = None,
) -> str:
    """Resolve the execution backend, preferring the installed env file.

    Imports stay inside the function: ``cli.backend`` imports this module at
    load time, so importing it at module level would create a cycle.
    """
    from openbase_coder_cli.cli.backend import read_backend
    from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

    if DEFAULT_ENV_FILE_PATH.is_file():
        configured_backend = read_backend(DEFAULT_ENV_FILE_PATH)
        if not configured_backend.startswith("unsupported:"):
            return execution_backend_for_configured_backend(configured_backend)
    if environment_backend is not None:
        return environment_backend()
    from super_agents.backend_clients import backend_from_environment

    return backend_from_environment()
