"""Managed backend binary updates must preserve the working executable on failure."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from openbase_coder_cli import backend_binaries


def _codex_release(monkeypatch, tmp_path: Path, contents: bytes) -> Path:
    source = tmp_path / "release" / "codex-test"
    source.parent.mkdir()
    source.write_bytes(contents)
    source.chmod(0o755)
    archive_path = tmp_path / "codex.tar.gz"
    with tarfile.open(archive_path, "w:gz") as archive:
        archive.add(source, arcname=source.name)
    target = backend_binaries._codex_release_target()
    release = {
        "assets": [
            {
                "name": f"codex-{target}.tar.gz",
                "browser_download_url": "https://example.invalid/codex.tar.gz",
            }
        ]
    }
    monkeypatch.setattr(
        backend_binaries.urllib.request,
        "urlopen",
        lambda *a, **k: io.BytesIO(json.dumps(release).encode()),
    )
    monkeypatch.setattr(
        backend_binaries.urllib.request,
        "urlretrieve",
        lambda _url, destination: shutil.copyfile(archive_path, destination),
    )
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setattr(backend_binaries, "OPENBASE_BIN_DIR", managed)
    installed = managed / "codex"
    installed.write_bytes(b"#!/bin/sh\necho codex-old\n")
    installed.chmod(0o755)
    return installed


def test_codex_failed_copy_preserves_working_binary(monkeypatch, tmp_path):
    installed = _codex_release(monkeypatch, tmp_path, b"#!/bin/sh\necho codex-new\n")
    before = installed.read_bytes()

    def interrupted_copy(_source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(backend_binaries.shutil, "copy2", interrupted_copy)
    with pytest.raises(OSError, match="disk full"):
        backend_binaries.refresh_openbase_bin_codex()
    assert installed.read_bytes() == before
    assert list(installed.parent.iterdir()) == [installed]


def test_codex_invalid_candidate_preserves_working_binary(monkeypatch, tmp_path):
    installed = _codex_release(monkeypatch, tmp_path, b"#!/bin/sh\nexit 1\n")
    before = installed.read_bytes()
    with pytest.raises(subprocess.CalledProcessError):
        backend_binaries.refresh_openbase_bin_codex()
    assert installed.read_bytes() == before
    assert list(installed.parent.iterdir()) == [installed]


def test_codex_refresh_replaces_only_validated_candidate(monkeypatch, tmp_path):
    contents = b"#!/bin/sh\necho codex-new\n"
    installed = _codex_release(monkeypatch, tmp_path, contents)
    before = installed.read_bytes()
    copy2 = shutil.copy2

    def inspect_copy(source, destination):
        result = copy2(source, destination)
        assert installed.read_bytes() == before
        return result

    monkeypatch.setattr(backend_binaries.shutil, "copy2", inspect_copy)
    assert backend_binaries.refresh_openbase_bin_codex() is True
    assert installed.read_bytes() == contents
    assert list(installed.parent.iterdir()) == [installed]


def test_codex_validation_timeout_preserves_working_binary(monkeypatch, tmp_path):
    installed = _codex_release(monkeypatch, tmp_path, b"#!/bin/sh\necho codex-new\n")
    before = installed.read_bytes()

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(backend_binaries.subprocess, "run", timeout)
    with pytest.raises(subprocess.TimeoutExpired):
        backend_binaries.refresh_openbase_bin_codex()
    assert installed.read_bytes() == before
    assert list(installed.parent.iterdir()) == [installed]


def test_codex_failed_atomic_replace_preserves_working_binary(monkeypatch, tmp_path):
    installed = _codex_release(monkeypatch, tmp_path, b"#!/bin/sh\necho codex-new\n")
    before = installed.read_bytes()

    def fail_replace(*args):
        raise PermissionError("replace denied")

    monkeypatch.setattr(backend_binaries.os, "replace", fail_replace)
    with pytest.raises(PermissionError, match="replace denied"):
        backend_binaries.refresh_openbase_bin_codex()
    assert installed.read_bytes() == before
    assert list(installed.parent.iterdir()) == [installed]
