from __future__ import annotations

from types import SimpleNamespace

import pytest

from openbase_coder_cli.voice_lockdown.keychain import (
    KEYCHAIN_SERVICE,
    KeychainRecord,
    KeychainUnavailableError,
    MacOSKeychain,
)


def _record():
    return KeychainRecord(False, "salt", "verifier", "audit-key")


def test_keychain_write_keeps_secret_json_out_of_argv(monkeypatch):
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("platform.system", lambda: "Darwin")
    MacOSKeychain(runner=runner).write(_record())
    argv, kwargs = calls[0]
    assert KEYCHAIN_SERVICE in argv
    assert _record().to_json() not in " ".join(argv)
    assert kwargs["input"] == _record().to_json()


def test_non_macos_has_no_file_or_environment_fallback(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Linux")
    with pytest.raises(KeychainUnavailableError):
        MacOSKeychain().read()
