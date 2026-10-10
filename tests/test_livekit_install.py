"""Tests for the pinned livekit-server dev installer."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from openbase_coder_cli import livekit_install, self_update, self_update_network
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

    monkeypatch.setattr(
        livekit_install, "_download_from_openbase_package", failing_download
    )

    assert livekit_install.ensure_pinned_livekit_server() is None


def test_install_refuses_version_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(livekit_install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: "arm64")
    staged = tmp_path / "staged-livekit-server"
    staged.write_text("#!/bin/sh\n", encoding="utf-8")
    monkeypatch.setattr(
        livekit_install, "_download_from_openbase_package", lambda _pin: staged
    )
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
    monkeypatch.setattr(
        livekit_install, "_download_from_openbase_package", lambda _pin: staged
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
def package_feed(tmp_path, monkeypatch):
    """Signed feeds plus real tar extraction, checksum and executable version checks."""
    monkeypatch.setattr(livekit_install, "OPENBASE_BIN_DIR", tmp_path / "bin")
    monkeypatch.setattr(livekit_install.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(livekit_install, "fallback_livekit_server_path", lambda: None)
    monkeypatch.setattr(self_update, "__version__", "0.52.0.dev1")
    key = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        self_update,
        "UPDATE_MANIFEST_PUBLIC_KEY_B64",
        base64.b64encode(key.public_key().public_bytes_raw()).decode(),
    )
    state = SimpleNamespace(requests=[], archives={}, documents={})

    def publish(
        channel,
        engine,
        *,
        target="aarch64-apple-darwin",
        checksum=None,
        member="./bin/livekit-server",
    ):
        tag = "v0.52.0.dev0" if channel == "staging" else "v0.51.0"
        base = f"https://github.com/openbase-community/openbase/releases/download/{tag}"
        url = f"{base}/openbase-coder-package-{target}.tar.gz"
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode="w:gz") as archive:
            content = f"#!/bin/sh\necho 'livekit-server version {engine}'\n".encode()
            info = tarfile.TarInfo(member)
            info.mode = 0o755
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        state.archives[url] = data.getvalue()
        manifest = json.dumps(
            {
                "manifest_schema": 1,
                "channel": channel,
                "version": tag[1:],
                "targets": {
                    target: {
                        "url": url,
                        "sha256": checksum
                        or hashlib.sha256(data.getvalue()).hexdigest(),
                    }
                },
            }
        ).encode()
        manifest_url = (
            f"{base}/update-manifest.json"
            if channel == "staging"
            else self_update.STABLE_MANIFEST_URL
        )
        state.documents[manifest_url] = manifest
        state.documents[manifest_url + ".sig"] = base64.b64encode(key.sign(manifest))
        if channel == "staging":
            state.documents[self_update.RELEASES_API_URL] = json.dumps(
                [
                    {"tag_name": "v0.53.0", "assets": []},
                    {
                        "tag_name": tag,
                        "assets": [
                            {
                                "name": "update-manifest.json",
                                "browser_download_url": manifest_url,
                            },
                            {
                                "name": "update-manifest.json.sig",
                                "browser_download_url": manifest_url + ".sig",
                            },
                        ],
                    },
                ]
            ).encode()
        return manifest_url

    def get(url):
        state.requests.append(url)
        return state.documents[url]

    def urlopen(url, *, timeout):
        state.requests.append(url)
        response = io.BytesIO(state.archives[url])
        response.headers = {"Content-Length": str(len(state.archives[url]))}
        return response

    monkeypatch.setattr(self_update, "_http_get", get)
    monkeypatch.setattr(self_update_network.urllib.request, "urlopen", urlopen)
    state.publish = publish
    publish("stable", "1.13.7")
    publish("staging", LIVEKIT_SERVER_PINNED_VERSION)
    return state


@pytest.mark.parametrize(
    "arch,target",
    [
        ("arm64", "aarch64-apple-darwin"),
        ("aarch64", "aarch64-apple-darwin"),
        ("x86_64", "x86_64-apple-darwin"),
    ],
)
def test_developer_install_uses_signed_staging_instead_of_mismatched_latest(
    package_feed,
    monkeypatch,
    arch,
    target,
):
    monkeypatch.setattr(livekit_install.platform, "machine", lambda: arch)
    package_feed.publish("staging", LIVEKIT_SERVER_PINNED_VERSION, target=target)
    result = livekit_install.ensure_pinned_livekit_server()
    assert result == livekit_install.installed_livekit_server_path()
    assert livekit_install._binary_version(result) == LIVEKIT_SERVER_PINNED_VERSION
    assert any(f"package-{target}.tar.gz" in url for url in package_feed.requests)
    assert not any("/latest/" in url for url in package_feed.requests)


def test_staging_backend_selects_staging_even_for_stable_source(
    package_feed, monkeypatch
):
    monkeypatch.setattr(self_update, "__version__", "0.51.0")
    monkeypatch.setenv(
        "OPENBASE_CODER_CLI_WEB_BACKEND_URL", livekit_install.STAGING_WEB_BACKEND_URL
    )
    assert livekit_install.ensure_pinned_livekit_server() is not None
    assert self_update.STABLE_MANIFEST_URL not in package_feed.requests


def test_stable_source_does_not_cross_channels_or_accept_old_engine(
    package_feed, monkeypatch
):
    monkeypatch.setattr(self_update, "__version__", "0.51.0")
    assert livekit_install.ensure_pinned_livekit_server() is None
    assert self_update.STABLE_MANIFEST_URL in package_feed.requests
    assert self_update.RELEASES_API_URL not in package_feed.requests
    assert not livekit_install.installed_livekit_server_path().exists()


@pytest.mark.parametrize(
    "failure", ["checksum", "signature", "engine", "target", "member", "channel"]
)
def test_invalid_package_never_replaces_existing_engine(package_feed, failure):
    installed = livekit_install.installed_livekit_server_path()
    installed.parent.mkdir()
    installed.write_text("#!/bin/sh\necho 'livekit-server version 1.13.7'\n")
    installed.chmod(0o755)
    original = installed.read_bytes()
    inode = installed.stat().st_ino
    kwargs = {}
    if failure == "checksum":
        kwargs["checksum"] = "0" * 64
    if failure == "target":
        kwargs["target"] = "x86_64-apple-darwin"
    if failure == "member":
        kwargs["member"] = "other/livekit-server"
    url = package_feed.publish(
        "staging",
        "1.13.7" if failure == "engine" else LIVEKIT_SERVER_PINNED_VERSION,
        **kwargs,
    )
    if failure == "signature":
        package_feed.documents[url] += b" "
    if failure == "channel":
        # Serve a validly signed stable document from the staging feed.
        package_feed.documents[url] = package_feed.documents[
            self_update.STABLE_MANIFEST_URL
        ]
        package_feed.documents[url + ".sig"] = package_feed.documents[
            self_update.STABLE_MANIFEST_URL + ".sig"
        ]
    assert livekit_install.ensure_pinned_livekit_server() is None
    assert installed.read_bytes() == original
    assert installed.stat().st_ino == inode
    if failure in ("signature", "target", "channel"):
        assert not any(url.endswith(".tar.gz") for url in package_feed.requests)


def test_stable_source_installs_matching_stable_engine(package_feed, monkeypatch):
    monkeypatch.setattr(self_update, "__version__", "0.51.0")
    package_feed.publish("stable", LIVEKIT_SERVER_PINNED_VERSION)
    result = livekit_install.ensure_pinned_livekit_server()
    assert result is not None
    assert livekit_install._binary_version(result) == LIVEKIT_SERVER_PINNED_VERSION
    assert self_update.STABLE_MANIFEST_URL in package_feed.requests
    assert self_update.RELEASES_API_URL not in package_feed.requests


def test_missing_checksum_is_rejected_before_archive_download(package_feed):
    package_feed.publish("staging", LIVEKIT_SERVER_PINNED_VERSION, checksum="invalid")
    assert livekit_install.ensure_pinned_livekit_server() is None
    assert not any(url.endswith(".tar.gz") for url in package_feed.requests)
