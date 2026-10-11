"""Recover a managed executable damaged by older in-place Codex refreshes."""

from __future__ import annotations

import logging
import threading
import time

from openbase_coder_cli.backend_binaries import (
    BACKEND_INSTALL_ERRORS,
    refresh_openbase_bin_codex,
)
from openbase_coder_cli.paths import OPENBASE_BIN_DIR

logger = logging.getLogger(__name__)
REPAIR_RETRY_SECONDS = 300.0
_last_attempt: float | None = None
_attempt_lock = threading.Lock()


def repair_unusable_managed_codex() -> bool:
    """Atomically repair our binary without restarting existing app servers.

    Older updaters could overwrite a mapped executable. On macOS the new
    executable can then be killed despite its valid on-disk signature. Idle
    version-skew reconciliation can resume once the binary is executable.
    """
    from openbase_coder_cli.services.codex_version_skew import (
        installed_codex_version,
        resolve_installed_codex,
    )

    binary = resolve_installed_codex()
    managed = OPENBASE_BIN_DIR / "codex"
    if binary is None or not managed.is_file() or binary.resolve() != managed.resolve():
        return False
    if installed_codex_version(binary) is not None:
        return False

    global _last_attempt
    now = time.monotonic()
    with _attempt_lock:
        if _last_attempt is not None and now - _last_attempt < REPAIR_RETRY_SECONDS:
            return False
        _last_attempt = now
    try:
        repaired = refresh_openbase_bin_codex()
    except BACKEND_INSTALL_ERRORS as exc:
        # Existing servers can keep running; retry after the cooldown.
        logger.warning("codex_managed_binary repair_failed error=%s", exc)
        return False
    if repaired:
        logger.info("codex_managed_binary repaired")
    return repaired
