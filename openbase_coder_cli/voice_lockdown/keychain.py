"""Authoritative macOS Keychain storage for voice-lockdown configuration."""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass
from typing import Any

KEYCHAIN_SERVICE = "cloud.openbase.coder.voice-lockdown.v1"
KEYCHAIN_ACCOUNT = "configuration"


class KeychainUnavailableError(RuntimeError):
    pass


class KeychainCorruptError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class KeychainRecord:
    enabled: bool
    salt: str
    verifier: str
    audit_key: str
    version: int = 1

    @classmethod
    def from_json(cls, raw: str) -> "KeychainRecord":
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise KeychainCorruptError("Voice-lockdown Keychain data is invalid JSON.") from exc
        if not isinstance(value, dict):
            raise KeychainCorruptError("Voice-lockdown Keychain data is not an object.")
        try:
            record = cls(
                enabled=value["enabled"],
                salt=value["salt"],
                verifier=value["verifier"],
                audit_key=value["auditKey"],
                version=value["version"],
            )
        except (KeyError, TypeError) as exc:
            raise KeychainCorruptError("Voice-lockdown Keychain data is incomplete.") from exc
        if (
            record.version != 1
            or not isinstance(record.enabled, bool)
            or not all(isinstance(item, str) and item for item in (record.salt, record.verifier, record.audit_key))
        ):
            raise KeychainCorruptError("Voice-lockdown Keychain data failed validation.")
        return record

    def to_json(self) -> str:
        return json.dumps(
            {
                "version": self.version,
                "enabled": self.enabled,
                "salt": self.salt,
                "verifier": self.verifier,
                "auditKey": self.audit_key,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


class MacOSKeychain:
    """Small injectable wrapper; there is deliberately no non-Keychain fallback."""

    def __init__(self, *, runner: Any = subprocess.run) -> None:
        self._runner = runner

    def _require_macos(self) -> None:
        if platform.system() != "Darwin":
            raise KeychainUnavailableError("Voice lockdown requires macOS Keychain.")

    def read(self) -> KeychainRecord | None:
        self._require_macos()
        result = self._runner(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise KeychainUnavailableError("Unable to read voice-lockdown state from macOS Keychain.")
        return KeychainRecord.from_json(result.stdout.strip())

    def write(self, record: KeychainRecord) -> None:
        self._require_macos()
        # With no value following -w, security reads the secret from stdin;
        # this keeps it out of argv and process listings.
        result = self._runner(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-U",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            input=record.to_json(),
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise KeychainUnavailableError("Unable to write voice-lockdown state to macOS Keychain.")
