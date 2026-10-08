"""Tests for the standalone self-updater (contract: workspace AUTO_UPDATE.md)."""

from __future__ import annotations

import hashlib
import json
import tarfile
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from openbase_coder_cli import self_update
from openbase_coder_cli.runtime import RuntimePackage
from openbase_coder_cli.services.installation import InstallationConfig
from openbase_coder_cli.sync_daemon import SYNC_ENGINE_BINARY_NAMES


def _make_fake_package(
    root: Path, *, version: str, python_version: str = "3.12.8"
) -> Path:
    (root / "bin").mkdir(parents=True)
    launcher = root / "bin" / "openbase-coder"
    launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    launcher.chmod(0o755)
    livekit = root / "bin" / "livekit-server"
    livekit.write_text("#!/bin/sh\n", encoding="utf-8")
    livekit.chmod(0o755)
    tunneld = root / "bin" / "openbase-tunneld"
    tunneld.write_text("#!/bin/sh\n", encoding="utf-8")
    tunneld.chmod(0o755)
    for name in SYNC_ENGINE_BINARY_NAMES:
        binary = root / "bin" / name
        binary.write_text("#!/bin/sh\n", encoding="utf-8")
        binary.chmod(0o755)
    (root / "openbase-coder-package.json").write_text(
        json.dumps(
            {
                "layoutVersion": 1,
                "version": version,
                "target": "aarch64-apple-darwin",
                "channel": "stable",
                "pythonVersion": python_version,
            }
        ),
        encoding="utf-8",
    )
    return root


def _make_release_tarball(tmp_path: Path, *, version: str) -> tuple[Path, str]:
    package_dir = _make_fake_package(tmp_path / "staging", version=version)
    tarball = tmp_path / f"openbase-coder-package-{version}.tar.gz"
    with tarfile.open(tarball, "w:gz") as archive:
        archive.add(package_dir, arcname="openbase-coder-package")
    sha256 = hashlib.sha256(tarball.read_bytes()).hexdigest()
    return tarball, sha256


def _patch_unsigned_build(monkeypatch) -> None:
    monkeypatch.setattr(self_update, "UPDATE_MANIFEST_PUBLIC_KEY_B64", "")


def _patch_standalone_layout(monkeypatch, tmp_path: Path) -> dict[str, Path]:
    _patch_unsigned_build(monkeypatch)
    layout = {
        "releases": tmp_path / "standalone" / "releases",
        "current": tmp_path / "standalone" / "current",
        "previous": tmp_path / "standalone" / "previous",
        "cache": tmp_path / "update-check.json",
    }
    monkeypatch.setattr(self_update, "STANDALONE_PACKAGES_DIR", tmp_path / "standalone")
    monkeypatch.setattr(self_update, "STANDALONE_RELEASES_DIR", layout["releases"])
    monkeypatch.setattr(self_update, "STANDALONE_CURRENT_DIR", layout["current"])
    monkeypatch.setattr(self_update, "STANDALONE_PREVIOUS_DIR", layout["previous"])
    monkeypatch.setattr(self_update, "UPDATE_CHECK_CACHE_PATH", layout["cache"])
    return layout


def test_run_self_update_refuses_dev_mode(monkeypatch) -> None:
    monkeypatch.setattr(self_update, "current_runtime_package", lambda: None)

    with pytest.raises(self_update.SelfUpdateError, match="development workspace"):
        self_update.run_self_update()


def test_check_for_update_reports_dev_mode(monkeypatch) -> None:
    monkeypatch.setattr(self_update, "current_runtime_package", lambda: None)

    check = self_update.check_for_update()

    assert check.update_available is False
    assert "git-managed" in check.detail


def test_fetch_manifest_refuses_newer_schema(monkeypatch) -> None:
    _patch_unsigned_build(monkeypatch)
    payload = json.dumps({"manifest_schema": 99, "version": "9.9.9"}).encode("utf-8")
    monkeypatch.setattr(self_update, "_http_get", lambda _url: payload)

    with pytest.raises(self_update.SelfUpdateError, match="schema 99"):
        self_update._fetch_manifest("stable")


def test_self_update_blocked_by_newer_layout(monkeypatch, tmp_path) -> None:
    _patch_standalone_layout(monkeypatch, tmp_path)
    old_root = _make_fake_package(tmp_path / "release-old", version="1.0.0")
    monkeypatch.setattr(
        self_update,
        "current_runtime_package",
        lambda: RuntimePackage(
            root=old_root, version="1.0.0", target="aarch64-apple-darwin"
        ),
    )
    manifest = {
        "manifest_schema": 1,
        "version": "2.0.0",
        "layout_version": 2,
        "targets": {},
    }
    monkeypatch.setattr(
        self_update, "_http_get", lambda _url: json.dumps(manifest).encode("utf-8")
    )

    result = self_update.run_self_update(report=lambda _msg: None)

    assert result.status == "blocked"
    assert "layout 2" in result.detail


def test_self_update_defers_during_voice_session(monkeypatch, tmp_path) -> None:
    _patch_standalone_layout(monkeypatch, tmp_path)
    old_root = _make_fake_package(tmp_path / "release-old", version="1.0.0")
    monkeypatch.setattr(
        self_update,
        "current_runtime_package",
        lambda: RuntimePackage(
            root=old_root, version="1.0.0", target="aarch64-apple-darwin"
        ),
    )
    manifest = {
        "manifest_schema": 1,
        "version": "2.0.0",
        "layout_version": 1,
        "targets": {"aarch64-apple-darwin": {"url": "x", "sha256": "x"}},
    }
    monkeypatch.setattr(
        self_update, "_http_get", lambda _url: json.dumps(manifest).encode("utf-8")
    )
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: True)

    result = self_update.run_self_update(report=lambda _msg: None)

    assert result.status == "deferred"
    assert "--force" in result.detail


@pytest.mark.parametrize("call_starts_during_download", [False, True])
@pytest.mark.parametrize("force", [False, True])
def test_self_update_flips_current_and_keeps_previous(
    monkeypatch, tmp_path, call_starts_during_download, force
) -> None:
    layout = _patch_standalone_layout(monkeypatch, tmp_path)
    old_root = layout["releases"] / "1.0.0-aarch64-apple-darwin"
    _make_fake_package(old_root, version="1.0.0")
    layout["current"].parent.mkdir(parents=True, exist_ok=True)
    layout["current"].symlink_to(old_root)

    tarball, sha256 = _make_release_tarball(tmp_path, version="2.0.0")
    manifest = {
        "manifest_schema": 1,
        "version": "2.0.0",
        "layout_version": 1,
        "targets": {
            "aarch64-apple-darwin": {
                "url": tarball.as_uri(),
                "sha256": sha256,
            }
        },
    }
    monkeypatch.setattr(
        self_update,
        "current_runtime_package",
        lambda: RuntimePackage(
            root=old_root, version="1.0.0", target="aarch64-apple-darwin"
        ),
    )
    monkeypatch.setattr(
        self_update, "_http_get", lambda _url: json.dumps(manifest).encode("utf-8")
    )
    voice_active = False
    download = self_update._download_and_extract

    def download_then_start_call(**kwargs):
        nonlocal voice_active
        root = download(**kwargs)
        voice_active = call_starts_during_download
        return root

    monkeypatch.setattr(self_update, "_download_and_extract", download_then_start_call)
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: voice_active)
    launcher_calls: list[list[str]] = []
    monkeypatch.setattr(
        self_update,
        "_run_launcher",
        lambda _launcher, args, report: launcher_calls.append(args) or True,
    )
    monkeypatch.setattr(self_update, "_refresh_backend_binaries", lambda report: None)

    result = self_update.run_self_update(force=force, report=lambda _msg: None)

    if call_starts_during_download and not force:
        assert result.status == "deferred"
        assert layout["current"].resolve() == old_root.resolve()
        assert not layout["previous"].exists()
        assert launcher_calls == []
        return

    assert result.status == "updated"
    assert result.to_version == "2.0.0"
    assert (
        layout["current"].resolve()
        == (layout["releases"] / "2.0.0-aarch64-apple-darwin").resolve()
    )
    assert layout["previous"].resolve() == old_root.resolve()
    assert ["services", "install"] in launcher_calls
    cache = json.loads(layout["cache"].read_text(encoding="utf-8"))
    assert cache["update_available"] is False


@pytest.mark.parametrize("activation_timeout", [False, True])
def test_self_update_rolls_back_on_failed_health_gate(
    monkeypatch, tmp_path, activation_timeout
) -> None:
    layout = _patch_standalone_layout(monkeypatch, tmp_path)
    old_root = layout["releases"] / "1.0.0-aarch64-apple-darwin"
    _make_fake_package(old_root, version="1.0.0")
    layout["current"].parent.mkdir(parents=True, exist_ok=True)
    layout["current"].symlink_to(old_root)

    tarball, sha256 = _make_release_tarball(tmp_path, version="2.0.0")
    manifest = {
        "manifest_schema": 1,
        "version": "2.0.0",
        "layout_version": 1,
        "targets": {
            "aarch64-apple-darwin": {"url": tarball.as_uri(), "sha256": sha256}
        },
    }
    monkeypatch.setattr(
        self_update,
        "current_runtime_package",
        lambda: RuntimePackage(
            root=old_root, version="1.0.0", target="aarch64-apple-darwin"
        ),
    )
    monkeypatch.setattr(
        self_update, "_http_get", lambda _url: json.dumps(manifest).encode("utf-8")
    )
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: False)

    # New launcher fails post-flip; old launcher succeeds during rollback.
    def failed_activation(_launcher, *, old_root, new_root, report, plugin_backup=None):
        if activation_timeout:
            import subprocess

            raise subprocess.TimeoutExpired("services-install", 600)
        return False

    monkeypatch.setattr(self_update, "_post_flip", failed_activation)
    monkeypatch.setattr(self_update, "_refresh_backend_binaries", lambda _: None)
    rollback_calls: list[list[str]] = []
    monkeypatch.setattr(
        self_update,
        "_run_launcher",
        lambda _launcher, args, report: rollback_calls.append(args) or True,
    )

    result = self_update.run_self_update(report=lambda _msg: None)

    assert result.status == "rolled-back"
    assert layout["current"].resolve() == old_root.resolve()
    assert ["services", "install"] in rollback_calls


def test_download_rejects_checksum_mismatch(monkeypatch, tmp_path) -> None:
    _patch_standalone_layout(monkeypatch, tmp_path)
    tarball, _sha256 = _make_release_tarball(tmp_path, version="2.0.0")

    with pytest.raises(self_update.SelfUpdateError, match="checksum mismatch"):
        self_update._download_and_extract(
            url=tarball.as_uri(),
            sha256="0" * 64,
            version="2.0.0",
            target="aarch64-apple-darwin",
            report=lambda _msg: None,
        )


def test_validate_release_dir_requires_sync_engine(tmp_path: Path) -> None:
    release = _make_fake_package(tmp_path / "release", version="2.0.0")
    (release / "bin" / "openbase-sync").unlink()

    with pytest.raises(self_update.SelfUpdateError, match="openbase-sync"):
        self_update._validate_release_dir(release)


def test_installation_config_refuses_newer_schema(tmp_path, monkeypatch) -> None:
    from openbase_coder_cli.services import installation

    config_path = tmp_path / "installation.json"
    config_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    monkeypatch.setattr(installation, "INSTALLATION_JSON_PATH", config_path)

    with pytest.raises(ValueError, match="newer Openbase Coder"):
        InstallationConfig.load()


def test_dispatcher_config_refuses_newer_schema(tmp_path) -> None:
    from openbase_coder_cli import dispatcher_config

    config_path = tmp_path / "dispatcher-config.json"
    config_path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

    with pytest.raises(ValueError, match="newer Openbase Coder"):
        dispatcher_config.read_dispatcher_config(config_path)


def test_dispatcher_config_writes_schema_version(tmp_path) -> None:
    from openbase_coder_cli import dispatcher_config

    config_path = tmp_path / "dispatcher-config.json"
    dispatcher_config.set_auto_link_personal_skills(True, config_path)

    payload = json.loads(config_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1


def test_version_info_ignores_packaged_cache_in_dev_mode(monkeypatch, tmp_path) -> None:
    cache_path = tmp_path / "update-check.json"
    cache_path.write_text(
        json.dumps({"update_available": True, "latest_version": "9.9.9"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(self_update, "UPDATE_CHECK_CACHE_PATH", cache_path)
    monkeypatch.setattr(self_update, "current_runtime_package", lambda: None)

    info = self_update.version_info()

    assert info["standalone"] is False
    assert info["update_available"] is False
    assert info["update_required"] is False
    assert "latest_version" not in info


def test_concurrent_self_update_defers(monkeypatch, tmp_path) -> None:
    import fcntl

    _patch_standalone_layout(monkeypatch, tmp_path)
    old_root = _make_fake_package(tmp_path / "release-old", version="1.0.0")
    monkeypatch.setattr(
        self_update,
        "current_runtime_package",
        lambda: RuntimePackage(
            root=old_root, version="1.0.0", target="aarch64-apple-darwin"
        ),
    )
    lock_path = tmp_path / "standalone" / ".self-update.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = lock_path.open("w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = self_update.run_self_update(report=lambda _msg: None)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        holder.close()

    assert result.status == "deferred"
    assert "already running" in result.detail


def test_manifest_signature_enforced_when_key_embedded(monkeypatch) -> None:
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key = Ed25519PrivateKey.generate()
    public_b64 = base64.b64encode(private_key.public_key().public_bytes_raw()).decode(
        "ascii"
    )
    monkeypatch.setattr(self_update, "UPDATE_MANIFEST_PUBLIC_KEY_B64", public_b64)

    manifest_bytes = json.dumps({"manifest_schema": 1, "version": "1.0.0"}).encode(
        "utf-8"
    )
    good_sig = base64.b64encode(private_key.sign(manifest_bytes))
    responses = {"manifest": manifest_bytes, "sig": good_sig}
    monkeypatch.setattr(
        self_update,
        "_http_get",
        lambda url: responses["sig"] if url.endswith(".sig") else responses["manifest"],
    )

    assert self_update._fetch_manifest("stable")["version"] == "1.0.0"

    responses["sig"] = base64.b64encode(b"0" * 64)
    with pytest.raises(self_update.SelfUpdateError, match="signature"):
        self_update._fetch_manifest("stable")


def test_auto_update_enabled_env_opt_out(monkeypatch) -> None:
    monkeypatch.delenv(self_update.AUTO_UPDATE_ENV_KEY, raising=False)
    assert self_update.auto_update_enabled() is True
    monkeypatch.setenv(self_update.AUTO_UPDATE_ENV_KEY, "0")
    assert self_update.auto_update_enabled() is False
    monkeypatch.setenv(self_update.AUTO_UPDATE_ENV_KEY, "false")
    assert self_update.auto_update_enabled() is False
    monkeypatch.setenv(self_update.AUTO_UPDATE_ENV_KEY, "1")
    assert self_update.auto_update_enabled() is True


@pytest.fixture
def standalone_updater(monkeypatch, tmp_path):
    package = RuntimePackage(
        root=tmp_path, version="1.0.0", target="aarch64-apple-darwin"
    )
    monkeypatch.setattr(self_update, "current_runtime_package", lambda: package)
    return package


def test_spawn_detached_self_update_launches_current_launcher(
    monkeypatch, tmp_path, standalone_updater
) -> None:
    _patch_standalone_layout(monkeypatch, tmp_path)
    release = _make_fake_package(tmp_path / "release-old", version="1.0.0")
    current = tmp_path / "standalone" / "current"
    current.parent.mkdir(parents=True, exist_ok=True)
    current.symlink_to(release)
    monkeypatch.setattr(
        self_update, "SELF_UPDATE_LOG_PATH", tmp_path / "logs" / "self-update.log"
    )

    spawned = []
    monkeypatch.setattr(
        self_update.subprocess,
        "Popen",
        lambda args, **kwargs: spawned.append((args, kwargs)),
    )

    self_update.spawn_detached_self_update()
    self_update.spawn_detached_self_update(force=True)

    assert spawned[0][0] == [
        str(current / "bin" / "openbase-coder"),
        "self-update",
        "--automatic",
    ]
    assert spawned[0][1]["start_new_session"] is True
    assert spawned[1][0][-1] == "--force"


def test_spawn_detached_self_update_requires_launcher(
    monkeypatch, tmp_path, standalone_updater
) -> None:
    _patch_standalone_layout(monkeypatch, tmp_path)
    with pytest.raises(self_update.SelfUpdateError, match="launcher"):
        self_update.spawn_detached_self_update()


def test_automatic_update_waits_for_call_without_fetching_feed(
    monkeypatch, tmp_path, standalone_updater
):
    monkeypatch.setattr(self_update, "OPENBASE_BASE_DIR", tmp_path)
    monkeypatch.setattr(InstallationConfig, "exists", lambda: False)
    monkeypatch.delenv(self_update.AUTO_UPDATE_ENV_KEY, raising=False)
    active = iter([True, True, False])
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: next(active))
    waits = []
    monkeypatch.setattr(self_update.time, "sleep", waits.append)
    updated = self_update.SelfUpdateResult("updated", "1.0.0", "2.0.0")
    applied = []

    def apply(**kwargs):
        applied.append(kwargs)
        return updated

    monkeypatch.setattr(self_update, "run_self_update", apply)
    assert self_update.run_automatic_self_update(report=lambda _: None) == updated
    assert waits == [60, 60]
    assert len(applied) == 1
    assert applied[0]["force"] is False


def test_pending_automatic_update_observes_new_opt_out(monkeypatch, tmp_path):
    monkeypatch.setattr(self_update, "OPENBASE_BASE_DIR", tmp_path)
    monkeypatch.setattr(InstallationConfig, "exists", lambda: False)
    monkeypatch.delenv(self_update.AUTO_UPDATE_ENV_KEY, raising=False)
    package = RuntimePackage(
        root=tmp_path, version="1.0.0", target="aarch64-apple-darwin"
    )
    monkeypatch.setattr(self_update, "current_runtime_package", lambda: package)
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: True)
    monkeypatch.setattr(
        self_update.time,
        "sleep",
        lambda _: (tmp_path / ".env").write_text("OPENBASE_CODER_AUTO_UPDATE=0\n"),
    )
    monkeypatch.setattr(
        self_update, "run_self_update", lambda **_: pytest.fail("opted-out update ran")
    )
    result = self_update.run_automatic_self_update(report=lambda _: None)
    assert result.status == "deferred"
    assert "disabled" in result.detail


@pytest.mark.parametrize("status", ["rolled-back", "blocked"])
def test_automatic_update_does_not_retry_failed_release(
    monkeypatch, tmp_path, status, standalone_updater
):
    monkeypatch.setattr(self_update, "OPENBASE_BASE_DIR", tmp_path)
    monkeypatch.setattr(InstallationConfig, "exists", lambda: False)
    monkeypatch.delenv(self_update.AUTO_UPDATE_ENV_KEY, raising=False)
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: False)
    failed = self_update.SelfUpdateResult(status, "1.0.0", "2.0.0")
    monkeypatch.setattr(self_update, "run_self_update", lambda **_: failed)
    monkeypatch.setattr(
        self_update.time, "sleep", lambda _: pytest.fail("failed release retried")
    )
    assert self_update.run_automatic_self_update(report=lambda _: None) == failed


def test_automatic_update_retries_a_deferred_race(
    monkeypatch, tmp_path, standalone_updater
):
    monkeypatch.setattr(self_update, "OPENBASE_BASE_DIR", tmp_path)
    monkeypatch.setattr(InstallationConfig, "exists", lambda: False)
    monkeypatch.delenv(self_update.AUTO_UPDATE_ENV_KEY, raising=False)
    monkeypatch.setattr(self_update, "_voice_session_active", lambda: False)
    updated = self_update.SelfUpdateResult("updated", "1.0.0", "2.0.0")
    results = iter([self_update.SelfUpdateResult("deferred", "1.0.0", None), updated])
    monkeypatch.setattr(self_update, "run_self_update", lambda **_: next(results))
    waits = []
    monkeypatch.setattr(self_update.time, "sleep", waits.append)
    assert self_update.run_automatic_self_update(report=lambda _: None) == updated
    assert waits == [60]


def test_automatic_update_reads_configured_env_file(
    monkeypatch, tmp_path, standalone_updater
):
    configured_env = tmp_path / "configured.env"
    configured_env.write_text("OPENBASE_CODER_AUTO_UPDATE=0\n")
    monkeypatch.setattr(InstallationConfig, "exists", lambda: True)
    monkeypatch.setattr(
        InstallationConfig,
        "load",
        lambda: SimpleNamespace(env_file=str(configured_env), standalone=True),
    )
    monkeypatch.setenv(self_update.AUTO_UPDATE_ENV_KEY, "1")
    monkeypatch.setattr(
        self_update, "_voice_session_active", lambda: pytest.fail("opt-out ignored")
    )
    assert self_update.run_automatic_self_update().status == "deferred"


@pytest.mark.parametrize("automatic", [False, True])
def test_cli_routes_automatic_attempts_without_changing_manual_updates(
    monkeypatch, automatic
):
    module = import_module("openbase_coder_cli.cli.self_update")
    calls = []
    result = self_update.SelfUpdateResult("up-to-date", "1.0.0", "1.0.0")
    monkeypatch.setattr(
        module, "run_self_update", lambda **_: calls.append("manual") or result
    )
    monkeypatch.setattr(
        module,
        "run_automatic_self_update",
        lambda **_: calls.append("automatic") or result,
    )
    args = ["--json"] + (["--automatic"] if automatic else [])
    invocation = CliRunner().invoke(module.self_update, args)
    assert invocation.exit_code == 0, invocation.output
    assert json.loads(invocation.output)["status"] == "up-to-date"
    assert calls == ["automatic" if automatic else "manual"]


def _fake_releases_response(monkeypatch, releases: list[dict]) -> None:
    def fake_http_get(url: str) -> bytes:
        assert url == self_update.RELEASES_API_URL
        return json.dumps(releases).encode()

    monkeypatch.setattr(self_update, "_http_get", fake_http_get)


def _release(tag: str, *, draft: bool = False, with_manifest: bool = True) -> dict:
    assets = []
    if with_manifest:
        assets = [
            {
                "name": self_update.MANIFEST_ASSET_NAME,
                "browser_download_url": f"https://example.test/{tag}/manifest",
            },
            {
                "name": self_update.MANIFEST_SIGNATURE_ASSET_NAME,
                "browser_download_url": f"https://example.test/{tag}/manifest.sig",
            },
        ]
    return {"tag_name": tag, "draft": draft, "assets": assets}


def test_staging_channel_resolves_only_dev_releases(monkeypatch) -> None:
    _fake_releases_response(
        monkeypatch,
        [
            _release("v0.4.0"),
            _release("v0.4.0.dev20260812120000"),
            _release("v0.3.0.dev20260811120000"),
        ],
    )

    manifest_url, signature_url = self_update._prerelease_manifest_urls("staging")

    assert manifest_url == "https://example.test/v0.4.0.dev20260812120000/manifest"
    assert signature_url.endswith("manifest.sig")


def test_beta_channel_skips_staging_releases(monkeypatch) -> None:
    _fake_releases_response(
        monkeypatch,
        [
            _release("v0.5.0.dev20260812120000"),
            _release("v0.4.0b1"),
            _release("v0.3.0"),
        ],
    )

    manifest_url, _ = self_update._prerelease_manifest_urls("beta")

    assert manifest_url == "https://example.test/v0.4.0b1/manifest"


def test_staging_channel_errors_without_staging_releases(monkeypatch) -> None:
    _fake_releases_response(monkeypatch, [_release("v0.3.0")])

    with pytest.raises(self_update.SelfUpdateError, match="staging"):
        self_update._prerelease_manifest_urls("staging")


def test_staging_channel_skips_drafts_and_manifestless_releases(monkeypatch) -> None:
    _fake_releases_response(
        monkeypatch,
        [
            _release("v0.5.0.dev20260813120000", draft=True),
            _release("v0.4.0.dev20260812120000", with_manifest=False),
            _release("v0.3.0.dev20260811120000"),
        ],
    )

    manifest_url, _ = self_update._prerelease_manifest_urls("staging")

    assert manifest_url == "https://example.test/v0.3.0.dev20260811120000/manifest"


@pytest.mark.parametrize("failure", ["plugins", "services", "timeout", None])
def test_python_minor_upgrade_preserves_plugins_on_activation_failure(
    tmp_path, monkeypatch, failure
):
    import subprocess

    old = _make_fake_package(tmp_path / "old", version="1.0", python_version="3.12.8")
    new = _make_fake_package(tmp_path / "new", version="2.0", python_version="3.13.1")
    site = tmp_path / "plugins" / "site"
    site.mkdir(parents=True)
    (site / "native.so").write_bytes(b"old-python-plugin")
    monkeypatch.setattr(self_update, "PLUGIN_SITE_DIR", site)
    calls = []

    def run(launcher, args, *, report):
        calls.append(args[0])
        if args[0] == "plugins":
            (site / "native.so").write_bytes(b"new-python-plugin")
        if failure == "timeout":
            raise subprocess.TimeoutExpired("plugin-rebuild", 600)
        return args[0] != failure

    monkeypatch.setattr(self_update, "_run_launcher", run)
    if failure == "timeout":
        with pytest.raises(subprocess.TimeoutExpired):
            self_update._post_flip(
                new / "bin/openbase-coder",
                old_root=old,
                new_root=new,
                report=lambda _s: None,
            )
    else:
        assert self_update._post_flip(
            new / "bin/openbase-coder",
            old_root=old,
            new_root=new,
            report=lambda _s: None,
        ) is (failure is None)
    assert (site / "native.so").read_bytes() == (
        b"new-python-plugin" if failure is None else b"old-python-plugin"
    )
    assert calls == (
        ["plugins"] if failure in ("plugins", "timeout") else ["plugins", "services"]
    )
    assert sorted(p.name for p in site.parent.iterdir()) == ["site"]
