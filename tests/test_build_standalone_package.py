from __future__ import annotations

import importlib.util
import json
import plistlib
import py_compile
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "build_standalone_package.py"
SPEC = importlib.util.spec_from_file_location("build_standalone_package", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
build_standalone_package = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(build_standalone_package)


def test_prune_rebuildable_bytecode_preserves_pyc_only_modules(tmp_path: Path) -> None:
    python_dir = tmp_path / "python"
    package_dir = python_dir / "lib" / "python3.12" / "site-packages" / "example"
    package_dir.mkdir(parents=True)

    source = package_dir / "source_backed.py"
    source.write_text("VALUE = 1\n", encoding="utf-8")
    source_bytecode = Path(py_compile.compile(str(source), doraise=True))

    pyc_only_source = package_dir / "pyc_only.py"
    pyc_only_source.write_text("VALUE = 2\n", encoding="utf-8")
    pyc_only_bytecode = Path(py_compile.compile(str(pyc_only_source), doraise=True))
    pyc_only_source.unlink()

    unrelated = source_bytecode.parent / "keep.txt"
    unrelated.write_text("not bytecode\n", encoding="utf-8")
    expected_freed = source_bytecode.stat().st_size

    count, freed_bytes = build_standalone_package.prune_rebuildable_bytecode(python_dir)

    assert count == 1
    assert freed_bytes == expected_freed
    assert not source_bytecode.exists()
    assert pyc_only_bytecode.exists()
    assert unrelated.exists()


def test_prune_rebuildable_bytecode_removes_empty_cache_directory(
    tmp_path: Path,
) -> None:
    python_dir = tmp_path / "python"
    source = python_dir / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    bytecode = Path(py_compile.compile(str(source), doraise=True))
    cache_dir = bytecode.parent

    build_standalone_package.prune_rebuildable_bytecode(python_dir)

    assert not cache_dir.exists()


def test_validate_no_rebuildable_bytecode_rejects_source_backed_cache(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    source = package_dir / "python" / "example.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    bytecode = Path(py_compile.compile(str(source), doraise=True))

    with pytest.raises(RuntimeError) as exc_info:
        build_standalone_package._validate_no_rebuildable_bytecode(package_dir)

    assert str(bytecode) in str(exc_info.value)


def test_verify_source_super_agents_rejects_incompatible_mcp_runtime(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    commands: list[list[str]] = []
    results = iter(
        [
            subprocess.CompletedProcess([], 0),
            subprocess.CompletedProcess([], 1),
        ]
    )

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess:
        commands.append(command)
        return next(results)

    monkeypatch.setattr(
        build_standalone_package,
        "runtime_python",
        lambda _python_dir: tmp_path / "python",
    )
    monkeypatch.setattr(build_standalone_package.subprocess, "run", fake_run)

    with pytest.raises(SystemExit, match="cannot construct its MCP server"):
        build_standalone_package._verify_source_super_agents(tmp_path)

    assert len(commands) == 2
    assert "create_server(object())" in commands[1][-1]


def test_stage_bin_includes_direct_tunnel_and_sync_engine(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    python_dir = package_dir / "python"
    livekit = tmp_path / "livekit-server"
    tunneld = tmp_path / "openbase-tunneld"
    sync_engine = tmp_path / "sync-engine"
    sync_engine.mkdir()
    livekit.write_bytes(b"livekit")
    tunneld.write_bytes(b"tunneld")
    for name in build_standalone_package.SYNC_ENGINE_BINARIES:
        (sync_engine / name).write_bytes(name.encode())

    build_standalone_package.stage_bin(
        package_dir,
        python_dir,
        livekit,
        tunneld,
        sync_engine,
    )

    assert (package_dir / "bin" / "livekit-server").read_bytes() == b"livekit"
    packaged_tunneld = package_dir / "bin" / "openbase-tunneld"
    assert packaged_tunneld.read_bytes() == b"tunneld"
    assert packaged_tunneld.stat().st_mode & 0o111
    for name in build_standalone_package.SYNC_ENGINE_BINARIES:
        packaged = package_dir / "bin" / name
        assert packaged.read_bytes() == name.encode()
        assert packaged.stat().st_mode & 0o111


def test_validate_package_requires_sync_engine_when_requested(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    for path in (
        package_dir / build_standalone_package.METADATA_FILENAME,
        package_dir / "bin" / "openbase-coder",
        package_dir / "bin" / "livekit-server",
        package_dir / "bin" / "openbase-tunneld",
        package_dir / "python" / "bin" / "python",
        package_dir / "console" / "index.html",
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    with pytest.raises(RuntimeError, match="openbase-syncd"):
        build_standalone_package.validate_package(
            package_dir,
            "1.0.0",
            require_sync_engine=True,
        )


def _fake_launcher_app(app: Path) -> Path:
    executable = app / "Contents" / "MacOS" / "openbase-services"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    (app / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleExecutable": "openbase-services",
                "CFBundleIdentifier": "cloud.openbase.coder.services",
            }
        )
    )
    return executable


def test_stage_service_launcher_copies_the_prebuilt_bundle_for_macos(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    source = tmp_path / "Openbase Services.app"
    _fake_launcher_app(source)

    relative = build_standalone_package.stage_service_launcher(
        package_dir, source, version="1.0.0", target="aarch64-apple-darwin"
    )

    assert relative == build_standalone_package.SERVICE_LAUNCHER_RELATIVE_PATH
    assert (package_dir / relative).is_file()
    assert (package_dir / relative).parents[1].joinpath("Info.plist").is_file()


def test_stage_service_launcher_rejects_bundles_without_an_identifier(
    tmp_path: Path,
) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()
    source = tmp_path / "Openbase Services.app"
    _fake_launcher_app(source)
    (source / "Contents" / "Info.plist").write_bytes(
        plistlib.dumps({"CFBundleExecutable": "openbase-services"})
    )

    with pytest.raises(RuntimeError, match="CFBundleIdentifier"):
        build_standalone_package.stage_service_launcher(
            package_dir, source, version="1.0.0", target="aarch64-apple-darwin"
        )


def test_stage_service_launcher_is_macos_only(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    assert (
        build_standalone_package.stage_service_launcher(
            package_dir, None, version="1.0.0", target="x86_64-unknown-linux-gnu"
        )
        is None
    )
    assert not (package_dir / "libexec").exists()


def test_metadata_records_the_service_launcher(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        build_standalone_package, "package_python_version", lambda _dir: "3.12.0"
    )
    build_standalone_package.write_metadata(
        tmp_path,
        version="1.0.0",
        target="aarch64-apple-darwin",
        channel="stable",
        repo_shas={},
        service_launcher=build_standalone_package.SERVICE_LAUNCHER_RELATIVE_PATH,
    )
    metadata = json.loads(
        (tmp_path / build_standalone_package.METADATA_FILENAME).read_text()
    )
    assert (
        metadata["serviceLauncher"]
        == "libexec/Openbase Services.app/Contents/MacOS/openbase-services"
    )

    build_standalone_package.write_metadata(
        tmp_path,
        version="1.0.0",
        target="x86_64-unknown-linux-gnu",
        channel="stable",
        repo_shas={},
    )
    metadata = json.loads(
        (tmp_path / build_standalone_package.METADATA_FILENAME).read_text()
    )
    assert "serviceLauncher" not in metadata


def test_required_package_files_include_the_service_launcher(tmp_path: Path) -> None:
    required = build_standalone_package.required_package_files(
        tmp_path, require_sync_engine=True, require_service_launcher=True
    )
    assert (
        tmp_path / build_standalone_package.SERVICE_LAUNCHER_RELATIVE_PATH in required
    )
    assert tmp_path / "bin" / "openbase-syncd" in required
    assert (
        tmp_path / build_standalone_package.SERVICE_LAUNCHER_RELATIVE_PATH
        not in build_standalone_package.required_package_files(tmp_path)
    )
