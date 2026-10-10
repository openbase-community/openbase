"""Tests for the pinned livekit-server dev installer."""

from __future__ import annotations

import hashlib
import io
import shutil
import subprocess
import tarfile
from dataclasses import replace

import pytest

from openbase_coder_cli import livekit_install
from openbase_coder_cli.livekit_artifacts import DarwinLiveKitArtifact
from openbase_coder_cli.livekit_version import LIVEKIT_SERVER_PINNED_VERSION


def test_ensure_skips_when_installed_binary_matches_pin(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed = bin_dir / "livekit-server"
    installed.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", bin_dir)
    monkeypatch.setattr(
        livekit_install,
        "_binary_version",
        lambda _binary: LIVEKIT_SERVER_PINNED_VERSION,
    )

    def unexpected_download(*_args, **_kwargs):
        raise AssertionError("must not download when the pin is installed")

    monkeypatch.setattr(livekit_install, "_extract_livekit_server", unexpected_download)

    assert livekit_install.ensure_pinned_livekit_server() == installed


def test_ensure_falls_back_to_none_when_download_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(livekit_install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: "arm64")

    def failing_download(_url, **_kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(livekit_install, "_extract_livekit_server", failing_download)
    monkeypatch.setattr(livekit_install, "fallback_livekit_server_path", lambda: None)

    assert livekit_install.ensure_pinned_livekit_server() is None


def test_install_refuses_version_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(livekit_install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: "arm64")
    staged = tmp_path / "staged-livekit-server"
    staged.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        livekit_install, "_extract_livekit_server", lambda _url, **_kwargs: staged
    )
    monkeypatch.setattr(livekit_install, "_binary_version", lambda _binary: "0.0.1")
    monkeypatch.setattr(livekit_install, "fallback_livekit_server_path", lambda: None)

    # The mismatch is caught inside ensure() and reported as a fallback.
    assert livekit_install.ensure_pinned_livekit_server() is None
    assert not (tmp_path / "bin" / "livekit-server").exists()


def test_ensure_uses_matching_fallback_when_openbase_package_lags_pin(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(livekit_install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: "arm64")
    staged = tmp_path / "staged-livekit-server"
    staged.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback = tmp_path / "homebrew-livekit-server"
    fallback.write_text("#!/bin/sh\n", encoding="utf-8")
    fallback.chmod(0o755)
    monkeypatch.setattr(
        livekit_install, "_extract_livekit_server", lambda _url, **_kwargs: staged
    )
    monkeypatch.setattr(
        livekit_install,
        "_binary_version",
        lambda binary: (
            LIVEKIT_SERVER_PINNED_VERSION if binary == fallback else "1.13.7"
        ),
    )
    monkeypatch.setattr(
        livekit_install, "fallback_livekit_server_path", lambda: fallback
    )

    assert livekit_install.ensure_pinned_livekit_server() == fallback
    assert not (tmp_path / "bin" / "livekit-server").exists()


def test_install_replaces_binary_with_fresh_inode(tmp_path, monkeypatch):
    """In-place overwrites of a running signed binary corrupt the kernel's
    cached code signature (execs then die with SIGKILL); the installer must
    rename a freshly staged copy into place instead."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    installed = bin_dir / "livekit-server"
    installed.write_text("old\n", encoding="utf-8")
    old_inode = installed.stat().st_ino
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", bin_dir)
    staged = tmp_path / "staged-livekit-server"
    staged.write_text("new\n", encoding="utf-8")
    monkeypatch.setattr(
        livekit_install,
        "_binary_version",
        lambda _binary: LIVEKIT_SERVER_PINNED_VERSION,
    )

    result = livekit_install._install_binary(staged, LIVEKIT_SERVER_PINNED_VERSION)

    assert result == installed
    assert installed.read_text(encoding="utf-8") == "new\n"
    assert installed.stat().st_ino != old_inode
    assert list(bin_dir.iterdir()) == [installed]


@pytest.fixture
def darwin_package(tmp_path, monkeypatch, request):
    """Real archive and executable through the setup entry point, without HTTP."""
    version = getattr(request, "param", "1.13.8")
    binary = f'#!/bin/sh\nprintf "livekit-server version {version}\\n"\n'.encode()
    package = tmp_path / "package.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        for name, data in [
            ("./bin/livekit-server", binary),
            ("other-file", b"ignored"),
        ]:
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    artifact = DarwinLiveKitArtifact(
        url="https://example.invalid/releases/download/v1/package.tar.gz",
        archive_sha256=hashlib.sha256(package.read_bytes()).hexdigest(),
        binary_sha256=hashlib.sha256(binary).hexdigest(),
    )
    calls = []

    def download(url, destination):
        calls.append(url)
        assert url == artifact.url
        shutil.copyfile(package, destination)

    monkeypatch.setattr(livekit_install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(livekit_install, "fallback_livekit_server_path", lambda: None)
    monkeypatch.setattr(livekit_install.urllib.request, "urlretrieve", download)
    monkeypatch.setattr(
        livekit_install, "DARWIN_LIVEKIT_ARTIFACTS", {("1.13.8", "aarch64"): artifact}
    )
    return artifact, calls


def test_darwin_setup_installs_verified_pin_without_path_fallback(darwin_package):
    artifact, calls = darwin_package
    installed = livekit_install.ensure_pinned_livekit_server()

    assert installed == livekit_install.installed_livekit_server_path()
    assert livekit_install._binary_version(installed) == "1.13.8"
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == artifact.binary_sha256
    assert calls == [artifact.url]
    assert livekit_install.ensure_pinned_livekit_server() == installed
    assert calls == [artifact.url]  # idempotent setup does not download again


@pytest.mark.parametrize("digest_field", ["archive_sha256", "binary_sha256"])
def test_darwin_checksum_failure_never_executes_or_replaces_binary(
    darwin_package, monkeypatch, digest_field
):
    artifact, _ = darwin_package
    monkeypatch.setattr(
        livekit_install,
        "DARWIN_LIVEKIT_ARTIFACTS",
        {("1.13.8", "aarch64"): replace(artifact, **{digest_field: "0" * 64})},
    )

    def unexpected_execution(_binary):
        pytest.fail("must reject unverified bytes before executing them")

    monkeypatch.setattr(livekit_install, "_binary_version", unexpected_execution)
    assert livekit_install.ensure_pinned_livekit_server() is None
    assert not livekit_install.installed_livekit_server_path().exists()


@pytest.mark.parametrize("darwin_package", ["1.13.7"], indirect=True)
def test_verified_bytes_still_require_matching_executable_version(darwin_package):
    assert livekit_install.ensure_pinned_livekit_server() is None
    assert not livekit_install.installed_livekit_server_path().exists()


@pytest.mark.parametrize("pin,arch", [("1.13.9", "arm64"), ("1.13.8", "x86_64")])
def test_missing_version_or_architecture_never_uses_latest(
    darwin_package, monkeypatch, pin, arch
):
    _, calls = darwin_package
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: arch)
    with pytest.raises(RuntimeError, match="no verified Darwin"):
        livekit_install._download_from_openbase_package(pin)
    assert calls == []


def test_declared_artifact_is_versioned_and_passes_both_checksums(monkeypatch):
    artifact = livekit_install.DARWIN_LIVEKIT_ARTIFACTS[
        (LIVEKIT_SERVER_PINNED_VERSION, "aarch64")
    ]
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: "aarch64")
    calls = []
    monkeypatch.setattr(
        livekit_install,
        "_extract_livekit_server",
        lambda *a, **kw: calls.append((a, kw)),
    )
    livekit_install._download_from_openbase_package(LIVEKIT_SERVER_PINNED_VERSION)
    assert "/releases/download/v" in artifact.url
    assert "/latest/" not in artifact.url
    assert calls == [
        (
            (artifact.url,),
            {
                "archive_sha256": artifact.archive_sha256,
                "binary_sha256": artifact.binary_sha256,
                "member_name": "bin/livekit-server",
            },
        )
    ]


@pytest.mark.parametrize("member_kind", ["symlink", "wrong_path", "duplicate"])
def test_extract_requires_one_regular_file_at_the_declared_path(
    tmp_path, monkeypatch, member_kind
):
    package = tmp_path / "bad.tar.gz"
    with tarfile.open(package, "w:gz") as archive:
        member = tarfile.TarInfo(
            "nested/bin/livekit-server"
            if member_kind == "wrong_path"
            else "bin/livekit-server"
        )
        if member_kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/unrelated/executable"
        archive.addfile(member)
        if member_kind == "duplicate":
            archive.addfile(member)
    monkeypatch.setattr(
        livekit_install.urllib.request,
        "urlretrieve",
        lambda _url, destination: shutil.copyfile(package, destination),
    )
    with pytest.raises(RuntimeError, match="expected one"):
        livekit_install._extract_livekit_server(
            "https://example.invalid/package.tar.gz", member_name="bin/livekit-server"
        )


@pytest.mark.parametrize(
    "stdout,code,expected",
    [
        ("livekit-server version 1.13.8\n", 0, "1.13.8"),
        ("livekit-server version 1.13.8\n", 1, None),
        ("error: requires Go 1.13.8", 0, None),
        ("livekit-server version 1.13.8-dev", 0, None),
    ],
)
def test_version_requires_successful_engine_version_output(
    monkeypatch, tmp_path, stdout, code, expected
):
    monkeypatch.setattr(
        livekit_install.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, code, stdout, ""),
    )
    assert livekit_install._binary_version(tmp_path / "livekit-server") == expected
