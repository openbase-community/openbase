"""Manage stable Openbase Cloud proxy machine tokens."""

from __future__ import annotations

import contextlib
import json
import os
import secrets
import socket
from collections.abc import Generator, Sequence

import httpx

from openbase_coder_cli.config.token_manager import (
    AuthLoginRequiredError,
    AuthTransientError,
    TokenManager,
)
from openbase_coder_cli.file_lock import LOCK_EX, LOCK_UN, flock
from openbase_coder_cli.paths import MACHINE_TOKEN_JSON_PATH

DEFAULT_MACHINE_TOKEN_SCOPES = ("llm_proxy", "audio_proxy")


class MachineTokenError(RuntimeError):
    """A machine token could not be minted or loaded."""


class MachineTokenManager:
    def __init__(self, web_backend_url: str, token_manager: TokenManager | None = None):
        self._web_backend_url = web_backend_url.rstrip("/")
        self._token_manager = token_manager or TokenManager(self._web_backend_url)

    @contextlib.contextmanager
    def _file_lock(self) -> Generator[None, None, None]:
        lock_path = MACHINE_TOKEN_JSON_PATH.with_suffix(".json.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            flock(fd, LOCK_EX)
            yield
        finally:
            flock(fd, LOCK_UN)
            os.close(fd)

    def get_machine_token(
        self,
        *,
        scopes: Sequence[str] = DEFAULT_MACHINE_TOKEN_SCOPES,
        rotate: bool = False,
    ) -> str:
        required_scopes = tuple(dict.fromkeys(scopes))
        with self._file_lock():
            if not rotate:
                cached = self._load()
                if self._cached_token_matches(cached, required_scopes):
                    return str(cached["token"])
            return self._mint(required_scopes)

    def clear(self) -> None:
        with self._file_lock():
            if MACHINE_TOKEN_JSON_PATH.is_file():
                MACHINE_TOKEN_JSON_PATH.unlink()

    def _load(self) -> dict:
        if not MACHINE_TOKEN_JSON_PATH.is_file():
            return {}
        try:
            data = json.loads(MACHINE_TOKEN_JSON_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _cached_token_matches(self, data: dict, scopes: Sequence[str]) -> bool:
        token = str(data.get("token") or "")
        cached_scopes = data.get("scopes")
        if not token.startswith("obmt_") or not isinstance(cached_scopes, list):
            return False
        if data.get("web_backend_url") != self._web_backend_url:
            return False
        return set(scopes).issubset({str(scope) for scope in cached_scopes})

    def _mint(self, scopes: Sequence[str]) -> str:
        access_token = self._access_token()
        install_id = self._install_id()
        try:
            response = httpx.post(
                f"{self._web_backend_url}/api/openbase/auth/machine-tokens/",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "name": socket.gethostname() or "Openbase Coder",
                    "install_id": install_id,
                    "scopes": list(scopes),
                },
                timeout=30,
            )
        except httpx.HTTPError as exc:
            raise AuthTransientError(f"Machine token mint failed: {exc}") from exc
        if response.status_code == 401:
            raise AuthLoginRequiredError(
                "Openbase Cloud rejected the current login while minting a machine token."
            )
        if response.status_code == 403:
            detail = _response_detail(response)
            raise MachineTokenError(
                f"Machine token mint was forbidden by Openbase Cloud: {detail}"
            )
        if response.status_code >= 500:
            raise AuthTransientError(
                f"Machine token mint failed with backend status {response.status_code}"
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            detail = _response_detail(response)
            raise MachineTokenError(
                f"Machine token mint failed with backend status {response.status_code}: {detail}"
            ) from exc

        payload = response.json()
        token = str(payload.get("token") or "")
        if not token.startswith("obmt_"):
            raise MachineTokenError("Machine token response did not include a token.")
        saved = {
            "web_backend_url": self._web_backend_url,
            "install_id": install_id,
            "token": token,
            "token_prefix": payload.get("token_prefix", token[:16]),
            "scopes": payload.get("scopes", list(scopes)),
        }
        MACHINE_TOKEN_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = MACHINE_TOKEN_JSON_PATH.with_suffix(f".json.tmp{os.getpid()}")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(saved, indent=2) + "\n")
            os.replace(tmp_path, MACHINE_TOKEN_JSON_PATH)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp_path)
        return token

    def _access_token(self) -> str:
        return self._token_manager.get_access_token()

    def _install_id(self) -> str:
        data = self._load()
        install_id = str(data.get("install_id") or "")
        if install_id:
            return install_id
        return f"openbase-coder-{secrets.token_urlsafe(24)}"


def _response_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300].strip() or response.reason_phrase
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error")
        if detail:
            return str(detail)
    return str(payload)[:300]
