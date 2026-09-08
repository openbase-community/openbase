"""Installation-scoped authentication capability for the local Coder API."""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path

from openbase_coder_cli.paths import OPENBASE_BASE_DIR

TOKEN_BYTES = 32
MIN_TOKEN_LENGTH = 40
LOCAL_API_TOKEN_PATH = OPENBASE_BASE_DIR / "local-api-token"


def _secure_mode(path: Path) -> None:
    """Keep the capability owner-readable only, including upgraded installs."""
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        path.chmod(0o600)


def _read_valid_token(path: Path) -> str | None:
    try:
        token = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return None
    _secure_mode(path)
    return token if len(token) >= MIN_TOKEN_LENGTH else None


def rotate_local_api_token(path: Path = LOCAL_API_TOKEN_PATH) -> str:
    """Atomically replace the installation capability with a fresh value."""
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(TOKEN_BYTES)
    temporary = path.with_name(
        f".{path.name}.tmp-{os.getpid()}-{secrets.token_hex(6)}"
    )
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
        os.replace(temporary, path)
        _secure_mode(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
    return token


def get_local_api_token(path: Path = LOCAL_API_TOKEN_PATH) -> str:
    """Read or create the local capability, repairing insecure file modes."""
    token = _read_valid_token(path)
    if token:
        return token

    path.parent.mkdir(parents=True, exist_ok=True)
    candidate = secrets.token_urlsafe(TOKEN_BYTES)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = _read_valid_token(path)
        if token:
            return token
        return rotate_local_api_token(path)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(candidate + "\n")
    _secure_mode(path)
    return candidate
