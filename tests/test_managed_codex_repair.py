from __future__ import annotations

import subprocess

import pytest

from openbase_coder_cli.services import codex_version_skew, managed_codex_repair


@pytest.fixture
def managed_binary(monkeypatch, tmp_path):
    binary = tmp_path / "codex"
    binary.write_bytes(b"unusable existing executable")
    monkeypatch.setattr(managed_codex_repair, "OPENBASE_BIN_DIR", tmp_path)
    monkeypatch.setattr(managed_codex_repair, "_last_attempt", None)
    monkeypatch.setattr(codex_version_skew, "resolve_installed_codex", lambda: binary)
    monkeypatch.setattr(codex_version_skew, "installed_codex_version", lambda _path: None)
    return binary


def test_repairs_unusable_managed_binary(managed_binary, monkeypatch):
    calls = []
    monkeypatch.setattr(
        managed_codex_repair, "refresh_openbase_bin_codex", lambda: calls.append(True) or True
    )
    assert managed_codex_repair.repair_unusable_managed_codex()
    assert calls == [True]


def test_healthy_binary_does_not_refresh(managed_binary, monkeypatch):
    monkeypatch.setattr(
        codex_version_skew, "installed_codex_version", lambda path: (str(path), "0.161.0")
    )
    monkeypatch.setattr(managed_codex_repair, "refresh_openbase_bin_codex", lambda: pytest.fail("unexpected refresh"))
    assert not managed_codex_repair.repair_unusable_managed_codex()


@pytest.mark.parametrize("absent", [False, True])
def test_does_not_install_missing_or_user_managed_binary(managed_binary, monkeypatch, absent):
    if absent:
        managed_binary.unlink()
    else:
        monkeypatch.setattr(codex_version_skew, "resolve_installed_codex", lambda: managed_binary.parent / "external-codex")
    monkeypatch.setattr(managed_codex_repair, "refresh_openbase_bin_codex", lambda: pytest.fail("unexpected refresh"))
    assert not managed_codex_repair.repair_unusable_managed_codex()


def test_failed_repair_preserves_binary_and_retries_after_cooldown(managed_binary, monkeypatch):
    now = [100.0]
    calls = []
    original = managed_binary.read_bytes()
    monkeypatch.setattr(managed_codex_repair.time, "monotonic", lambda: now[0])

    def refresh():
        calls.append(True)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired("codex --version", 30)
        return True

    monkeypatch.setattr(managed_codex_repair, "refresh_openbase_bin_codex", refresh)
    assert not managed_codex_repair.repair_unusable_managed_codex()
    assert managed_binary.read_bytes() == original
    assert not managed_codex_repair.repair_unusable_managed_codex()
    assert len(calls) == 1
    now[0] += managed_codex_repair.REPAIR_RETRY_SECONDS
    assert managed_codex_repair.repair_unusable_managed_codex()
    assert len(calls) == 2
