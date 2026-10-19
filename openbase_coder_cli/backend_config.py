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
# Opt-in to mixed-backend mode: a comma-separated list (e.g. "codex,claude_code")
# of backends this install runs SIMULTANEOUSLY — threads from every listed
# backend appear together and the dispatcher can manage super-agents on each.
# Unset = the single OPENBASE_CODING_BACKEND rules everything, as before.
CODING_BACKENDS_ENV_KEY = "OPENBASE_CODING_BACKENDS"
SUPER_AGENTS_DEFAULT_BACKEND_ENV_KEY = "SUPER_AGENTS_DEFAULT_BACKEND"
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


def backend_identity_for_execution_backend(
    configured_backend: str,
    execution_backend: str,
) -> str:
    """Preserve a configured provider identity when it uses this engine."""
    if (
        execution_backend_for_configured_backend(configured_backend)
        == execution_backend
    ):
        return configured_backend
    return execution_backend


def configured_backend_identity() -> str:
    """The configured backend identity, preferring the installed env file.

    A backend switch rewrites the env file and restarts services, but a
    process that was already running (django-cli serving the switch itself,
    or a detached restart it spawned) still carries the previous
    ``OPENBASE_CODING_BACKEND`` in its environment. Role models and client
    identities resolved from that stale value picked the old provider's
    defaults (Maritime pk 54, 2026-10-17: a claude_code Dispatcher recreated
    on the openbase_cloud role model, haiku). The env file is the source of
    truth whenever it exists; the environment only decides without one.
    """
    from openbase_coder_cli.cli.backend import read_env_values
    from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

    if DEFAULT_ENV_FILE_PATH.is_file():
        raw_value = read_env_values(DEFAULT_ENV_FILE_PATH).get(CODING_BACKEND_ENV_KEY)
        if raw_value:
            try:
                return normalize_backend(raw_value)
            except ValueError:
                pass
    from super_agents.backend_config import configured_backend_from_environment

    return configured_backend_from_environment()


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


_SYNCED_ENV_FILE_STAMP: tuple[int, int] | None = None
_ENV_FILE_SYNCED_KEYS = (CODING_BACKEND_ENV_KEY, CODING_BACKENDS_ENV_KEY)


def sync_process_env_from_env_file(*, force: bool = False) -> bool:
    """Copy the backend keys of the installed env file into ``os.environ``.

    A backend switch rewrites the env file while django-cli, the voice
    worker and the detached restart runner keep running with the environment
    they started with; the super-agents helpers (default backend, client
    identity, Claude session store) read ``os.environ``. Keeping the process
    environment in step with the file lets a switch take effect in place,
    without the service restarts that made it take 20 to 30 s (switch-speed,
    2026-10-18). Only keys present in the file are copied, so a test's empty
    env file leaves a monkeypatched environment alone. Returns whether the
    environment changed. The file's mtime and size gate the read so callers
    can invoke this on every lookup.
    """
    global _SYNCED_ENV_FILE_STAMP
    import os

    from openbase_coder_cli.env_file import env_file_values
    from openbase_coder_cli.paths import DEFAULT_ENV_FILE_PATH

    try:
        stat = DEFAULT_ENV_FILE_PATH.stat()
    except OSError:
        return False
    stamp = (stat.st_mtime_ns, stat.st_size)
    if not force and stamp == _SYNCED_ENV_FILE_STAMP:
        return False
    _SYNCED_ENV_FILE_STAMP = stamp
    values = env_file_values(DEFAULT_ENV_FILE_PATH)
    changed = False
    for key in _ENV_FILE_SYNCED_KEYS:
        value = values.get(key)
        if value is None or os.environ.get(key) == value:
            continue
        os.environ[key] = value
        changed = True
    return changed


def configured_execution_backends(
    environment_backend: Callable[[], str] | None = None,
) -> list[str]:
    """Every execution backend this install runs, primary first.

    ``OPENBASE_CODING_BACKENDS`` (comma-separated) opts a machine into
    mixed-backend mode: threads from every listed backend show together and
    the dispatcher can manage super-agents on each. Unset, the single
    configured backend rules everything, exactly as before. Invalid entries
    are skipped rather than failing thread listing.
    """
    import os

    sync_process_env_from_env_file()
    primary = configured_execution_backend(environment_backend)
    raw = os.environ.get(CODING_BACKENDS_ENV_KEY, "").strip()
    if not raw:
        return [primary]

    backends: list[str] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            configured = normalize_backend(chunk)
        except ValueError:
            continue
        execution = execution_backend_for_configured_backend(configured)
        if execution not in backends:
            backends.append(execution)
    if not backends:
        return [primary]
    # Keep the configured single backend as the primary (first) entry so
    # creation defaults and delegated behavior stay stable.
    if primary in backends:
        backends.remove(primary)
    return [primary, *backends]


# Openbase workers own introductions through their installed instructions,
# where they can honor task-specific silence and direct voice reply ownership.
# Disable the generic pre-prompt hook: it cannot inspect those constraints and
# would duplicate the worker's hello. Standalone Super Agents keeps its hook.
SUPER_AGENTS_THREAD_INTRO_COMMAND_ENV = "SUPER_AGENTS_THREAD_INTRO_COMMAND"
OPENBASE_THREAD_INTRO_COMMAND = ""
