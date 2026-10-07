"""Tests for the pinned livekit-server dev installer."""

from __future__ import annotations

from openbase_coder_cli import livekit_install
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

    def failing_download(_url):
        raise RuntimeError("offline")

    monkeypatch.setattr(livekit_install, "_extract_livekit_server", failing_download)

    assert livekit_install.ensure_pinned_livekit_server() is None


def test_install_refuses_version_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(livekit_install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: "arm64")
    staged = tmp_path / "staged-livekit-server"
    staged.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(livekit_install, "_extract_livekit_server", lambda _url: staged)
    monkeypatch.setattr(livekit_install, "_binary_version", lambda _binary: "0.0.1")

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
    monkeypatch.setattr(livekit_install, "_extract_livekit_server", lambda _url: staged)
    monkeypatch.setattr(
        livekit_install,
        "_binary_version",
        lambda binary: (
            LIVEKIT_SERVER_PINNED_VERSION if binary == fallback else "1.13.7"
        ),
    )
    monkeypatch.setattr(livekit_install, "fallback_livekit_server_path", lambda: fallback)

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
