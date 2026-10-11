from __future__ import annotations

import json
from pathlib import Path

from openbase_coder_cli import runtime
from openbase_coder_cli.services.installation import InstallationConfig


def test_runtime_package_resolves_from_explicit_env(monkeypatch, tmp_path: Path):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    (package_dir / runtime.PACKAGE_METADATA_FILENAME).write_text(
        json.dumps({"version": "1.2.3", "target": "aarch64-apple-darwin"}),
        encoding="utf-8",
    )

    monkeypatch.setenv("OPENBASE_CODER_PACKAGE_DIR", str(package_dir))
    monkeypatch.setattr(
        runtime,
        "__file__",
        str(package_dir / "python/lib/openbase_coder_cli/runtime.py"),
    )

    package = runtime.current_runtime_package()

    assert package is not None
    assert package.root == package_dir
    assert package.version == "1.2.3"
    assert package.console_build_dir == package_dir / "console"


def test_installation_config_load_ignores_unknown_keys(monkeypatch, tmp_path: Path):
    install_path = tmp_path / "installation.json"
    install_path.write_text(
        json.dumps(
            {
                "workspace_path": "",
                "env_file": "/tmp/.env",
                "standalone": True,
                "unknown": "ignored",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "openbase_coder_cli.services.installation.INSTALLATION_JSON_PATH",
        install_path,
    )

    config = InstallationConfig.load()

    assert config.workspace_path == ""
    assert config.env_file == "/tmp/.env"
    assert config.standalone is True


def test_stable_package_path_routes_through_current_symlink(monkeypatch, tmp_path):
    release = tmp_path / "releases" / "1.0.0"
    (release / "python" / "bin").mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(release)
    monkeypatch.setattr(runtime, "STANDALONE_CURRENT_DIR", current)

    pinned = release / "python" / "bin" / "super-agents-mcp"

    assert (
        runtime.stable_package_path(pinned)
        == current / "python" / "bin" / "super-agents-mcp"
    )


def test_stable_package_path_leaves_other_releases_unchanged(monkeypatch, tmp_path):
    release = tmp_path / "releases" / "1.0.0"
    other = tmp_path / "releases" / "0.9.0" / "python"
    release.mkdir(parents=True)
    other.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(release)
    monkeypatch.setattr(runtime, "STANDALONE_CURRENT_DIR", current)

    assert runtime.stable_package_path(other) == other


def test_stable_package_path_without_current_symlink(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime, "STANDALONE_CURRENT_DIR", tmp_path / "current")
    path = tmp_path / "somewhere" / "bin"

    assert runtime.stable_package_path(path) == path


def test_runtime_package_exposes_the_declared_service_launcher(
    monkeypatch, tmp_path: Path
):
    package_dir = tmp_path / "pkg"
    package_dir.mkdir()
    relative = "libexec/Openbase Services.app/Contents/MacOS/openbase-services"
    (package_dir / runtime.PACKAGE_METADATA_FILENAME).write_text(
        json.dumps(
            {
                "version": "1.2.3",
                "target": "aarch64-apple-darwin",
                "serviceLauncher": relative,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENBASE_CODER_PACKAGE_DIR", str(package_dir))
    monkeypatch.setattr(
        runtime,
        "__file__",
        str(package_dir / "python/lib/openbase_coder_cli/runtime.py"),
    )

    package = runtime.current_runtime_package()

    assert package is not None
    assert package.service_launcher == relative
    assert package.service_launcher_path == package_dir / relative
    # Packages without a launcher (older releases) expose none.
    assert runtime.RuntimePackage(root=package_dir).service_launcher_path is None


def test_stable_runtime_package_keeps_the_service_launcher(monkeypatch, tmp_path: Path):
    releases = tmp_path / "releases"
    release = releases / "1.2.3-aarch64-apple-darwin"
    release.mkdir(parents=True)
    current = tmp_path / "current"
    current.symlink_to(release)
    relative = "libexec/Openbase Services.app/Contents/MacOS/openbase-services"
    package = runtime.RuntimePackage(
        root=release,
        version="1.2.3",
        target="aarch64-apple-darwin",
        service_launcher=relative,
    )
    monkeypatch.setattr(runtime, "STANDALONE_CURRENT_DIR", current)
    monkeypatch.setattr(runtime, "current_runtime_package", lambda: package)

    stable = runtime.stable_runtime_package()

    assert stable is not None
    assert stable.root == current
    assert stable.service_launcher_path == current / relative
