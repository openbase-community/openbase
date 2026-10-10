"""The Openbase Services launcher: the macOS launchd job process whose signed
bundle identity keeps TCC grants stable across runtime updates
(dev-docs/MACOS_SERVICE_IDENTITY.md)."""

from __future__ import annotations

import importlib.util
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).parents[1] / "scripts"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve string annotations through sys.modules.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


build_service_launcher = _load_script("build_service_launcher")
sign_standalone_package = _load_script("sign_standalone_package")
build_standalone_package = _load_script("build_standalone_package")

SERVICES_BUNDLE_ID = "cloud.openbase.coder.services"
LAUNCHER_RELATIVE_PATH = build_standalone_package.SERVICE_LAUNCHER_RELATIVE_PATH


def _clang_available() -> bool:
    if sys.platform != "darwin" or shutil.which("xcrun") is None:
        return False
    probe = subprocess.run(
        ["xcrun", "--find", "clang"], check=False, capture_output=True, text=True
    )
    return probe.returncode == 0


needs_clang = pytest.mark.skipif(
    not _clang_available(), reason="needs macOS with the Xcode command-line tools"
)


# --- bundle metadata -----------------------------------------------------------


def test_info_plist_declares_the_stable_services_identity() -> None:
    info = plistlib.loads(build_service_launcher.render_info_plist(version="1.2.3"))

    # The bundle identifier IS the TCC client identity; it must never drift.
    assert info["CFBundleIdentifier"] == SERVICES_BUNDLE_ID
    assert info["CFBundleExecutable"] == build_service_launcher.EXECUTABLE_NAME
    assert info["CFBundlePackageType"] == "APPL"
    assert info["CFBundleShortVersionString"] == "1.2.3"
    assert info["CFBundleVersion"] == "1.2.3"
    # A background launcher: never a Dock icon or a window.
    assert info["LSUIElement"] is True
    assert info["LSBackgroundOnly"] is True
    # The prompts macOS shows for the services and their agent children.
    for key in (
        "NSDesktopFolderUsageDescription",
        "NSDocumentsFolderUsageDescription",
        "NSDownloadsFolderUsageDescription",
        "NSRemovableVolumesUsageDescription",
        "NSNetworkVolumesUsageDescription",
        "NSLocalNetworkUsageDescription",
    ):
        assert info[key].strip(), key


def test_launcher_layout_constants_agree() -> None:
    expected = (
        f"libexec/{build_standalone_package.SERVICE_LAUNCHER_APP_NAME}"
        f"/Contents/MacOS/{build_service_launcher.EXECUTABLE_NAME}"
    )
    assert LAUNCHER_RELATIVE_PATH == expected


def test_architecture_for_target_covers_release_targets() -> None:
    assert (
        build_service_launcher.architecture_for_target("aarch64-apple-darwin")
        == "arm64"
    )
    assert (
        build_service_launcher.architecture_for_target("x86_64-apple-darwin")
        == "x86_64"
    )
    with pytest.raises(RuntimeError, match="Unsupported launcher target"):
        build_service_launcher.architecture_for_target("x86_64-unknown-linux-gnu")


# --- the compiled launcher -----------------------------------------------------


@pytest.fixture(scope="module")
def launcher_app(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not _clang_available():
        pytest.skip("needs macOS with the Xcode command-line tools")
    app = tmp_path_factory.mktemp("launcher") / "Openbase Services.app"
    build_service_launcher.build_launcher_app(
        app, version="9.9.9", target="aarch64-apple-darwin"
    )
    return app


@pytest.fixture(scope="module")
def launcher(launcher_app: Path) -> Path:
    return launcher_app / "Contents" / "MacOS" / build_service_launcher.EXECUTABLE_NAME


@needs_clang
def test_built_bundle_has_the_app_bundle_layout(
    launcher_app: Path, launcher: Path
) -> None:
    assert launcher.is_file() and os.access(launcher, os.X_OK)
    assert (launcher_app / "Contents" / "PkgInfo").read_bytes() == b"APPL????"
    info = plistlib.loads((launcher_app / "Contents" / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == SERVICES_BUNDLE_ID
    assert info["CFBundleVersion"] == "9.9.9"


@needs_clang
def test_launcher_propagates_the_child_exit_status(launcher: Path) -> None:
    assert subprocess.run([str(launcher), "sh", "-c", "exit 3"]).returncode == 3
    assert subprocess.run([str(launcher), "true"]).returncode == 0


@needs_clang
def test_launcher_passes_environment_cwd_and_arguments(
    launcher: Path, tmp_path: Path
) -> None:
    result = subprocess.run(
        [
            str(launcher),
            "sh",
            "-c",
            'printf "%s|%s|%s" "$PWD" "$OPENBASE_TEST" "$1"',
            "sh",
            "arg one",
        ],
        cwd=tmp_path,
        env={**os.environ, "OPENBASE_TEST": "inherited"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout == f"{tmp_path.resolve()}|inherited|arg one"


@needs_clang
def test_launcher_reports_an_unstartable_command(launcher: Path) -> None:
    result = subprocess.run(
        [str(launcher), "/nonexistent/openbase-command"], capture_output=True, text=True
    )
    assert result.returncode == 127
    assert "cannot start /nonexistent/openbase-command" in result.stderr


@needs_clang
def test_launcher_without_a_command_is_a_usage_error(launcher: Path) -> None:
    assert subprocess.run([str(launcher)], capture_output=True).returncode == 64


def _wait_for(predicate, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


@needs_clang
def test_launcher_forwards_sigterm_and_dies_the_same_way(
    launcher: Path, tmp_path: Path
) -> None:
    # The child records its pid, then sleeps; a forwarded SIGTERM ends it and
    # the launcher must report "terminated by SIGTERM" like launchd expects.
    pid_file = tmp_path / "child.pid"
    process = subprocess.Popen(
        [str(launcher), "sh", "-c", f'echo $$ > "{pid_file}"; exec sleep 60']
    )
    assert _wait_for(pid_file.is_file)
    child_pid = int(pid_file.read_text().strip())

    process.send_signal(signal.SIGTERM)
    assert process.wait(timeout=10) == -signal.SIGTERM

    def child_gone() -> bool:
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            return True
        return False

    assert _wait_for(child_gone)


@needs_clang
def test_launcher_forwards_sigterm_received_during_spawn(tmp_path: Path) -> None:
    source = tmp_path / "spawn_signal.c"
    source.write_text(
        '#include <spawn.h>\n'
        'int spawn_with_signal(pid_t *, const char *, '
        'const posix_spawn_file_actions_t *, const posix_spawnattr_t *, '
        'char *const [], char *const []);\n'
        '#define posix_spawnp spawn_with_signal\n'
        '#include "openbase-services.c"\n'
        '#undef posix_spawnp\n'
        'int spawn_with_signal(pid_t *pid, const char *path, '
        'const posix_spawn_file_actions_t *actions, const posix_spawnattr_t *attributes, '
        'char *const argv[], char *const envp[]) {\n'
        '    int result = posix_spawnp(pid, path, actions, attributes, argv, envp);\n'
        '    if (result == 0) kill(getpid(), SIGTERM);\n'
        '    return result;\n'
        '}\n',
        encoding="utf-8",
    )
    executable = tmp_path / "launcher"
    subprocess.run(
        [
            "xcrun", "clang", "-Wall", "-Wextra", "-Werror",
            "-I", str(build_service_launcher.SOURCE_DIR),
            str(source), "-o", str(executable),
        ],
        check=True,
    )

    result = subprocess.run([str(executable), "/bin/sleep", "1"], timeout=10)

    assert result.returncode == -signal.SIGTERM


@needs_clang
def test_launcher_keeps_the_child_in_its_process_group(launcher: Path) -> None:
    # launchd reaps a job's whole process group when the job ends, which is
    # what cleans up the service if the launcher itself is SIGKILLed. With
    # start_new_session the launcher is the group leader, so the child's
    # pgid must equal the launcher's pid.
    process = subprocess.Popen(
        [str(launcher), "sh", "-c", "ps -o pgid= -p $$"],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    stdout, _ = process.communicate(timeout=10)
    assert process.returncode == 0
    assert int(stdout.strip()) == process.pid


@needs_clang
def test_bundle_signs_as_a_unit_with_the_services_identifier(
    launcher_app: Path, tmp_path: Path
) -> None:
    if shutil.which("codesign") is None:
        pytest.skip("codesign unavailable")
    bundle = tmp_path / "Openbase Services.app"
    shutil.copytree(launcher_app, bundle, symlinks=True)
    subprocess.run(["codesign", "--force", "--sign", "-", str(bundle)], check=True)
    info = subprocess.run(
        ["codesign", "-dvv", str(bundle)], check=True, capture_output=True, text=True
    )
    # codesign reports on stderr.
    assert f"Identifier={SERVICES_BUNDLE_ID}" in info.stderr
    subprocess.run(
        ["codesign", "--verify", "--strict", "--deep", str(bundle)], check=True
    )


# --- signing plan ------------------------------------------------------------


def _touch(path: Path, *, executable: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    if executable:
        path.chmod(0o755)
    return path


def _fake_package(root: Path) -> dict[str, Path]:
    """A package tree shaped like a real release (see the Mach-O inventory)."""
    files = {
        "python": _touch(root / "python/bin/python3.12", executable=True),
        "uv": _touch(root / "python/bin/uv", executable=True),
        "libpython": _touch(root / "python/lib/libpython3.12.dylib"),
        "tcl": _touch(root / "python/lib/itcl4.3.8/libitcl4.3.8.dylib"),
        "lib_dynload": _touch(
            root / "python/lib/python3.12/lib-dynload/_ssl.cpython-312-darwin.so"
        ),
        "site_packages": _touch(
            root
            / "python/lib/python3.12/site-packages/cryptography/hazmat/bindings/_rust.abi3.so"
        ),
        "tunneld": _touch(root / "bin/openbase-tunneld", executable=True),
        "livekit": _touch(root / "bin/livekit-server", executable=True),
        "syncd": _touch(root / "bin/openbase-syncd", executable=True),
        "launcher": _touch(root / LAUNCHER_RELATIVE_PATH, executable=True),
        "shim": _touch(root / "bin/openbase-coder", executable=True),  # shell script
        "script": _touch(root / "python/bin/pip", executable=True),  # shell trampoline
        "text": _touch(root / "python/lib/python3.12/os.py"),
    }
    (root / "libexec/Openbase Services.app/Contents/Info.plist").write_bytes(
        build_service_launcher.render_info_plist(version="1.0.0")
    )
    (root / "python/bin/python").symlink_to("python3.12")
    (root / "python/bin/python3").symlink_to("python3.12")
    return files


def _fake_is_macho(path: Path) -> bool:
    # Everything executable or library-shaped is Mach-O except the shell
    # script shim and the pip trampoline.
    return path.name not in {"openbase-coder", "pip"}


def test_plan_covers_every_macho_file_outside_bundles_and_seals_bundles(
    tmp_path: Path,
) -> None:
    files = _fake_package(tmp_path)

    plan = sign_standalone_package.plan_signing(tmp_path, is_macho=_fake_is_macho)

    machos = {
        key: path
        for key, path in files.items()
        if key not in {"shim", "script", "text"}
    }
    for key, path in machos.items():
        assert plan.covers(path), key
    # The launcher is sealed by its bundle, never signed as a loose file.
    assert files["launcher"] not in plan.files
    assert plan.bundles == (tmp_path / "libexec" / "Openbase Services.app",)
    # Non-Mach-O executables and symlinks are left alone.
    assert files["shim"] not in plan.files
    assert files["script"] not in plan.files
    assert files["text"] not in plan.files
    assert not any(path.is_symlink() for path in plan.files)


def test_plan_signs_nested_libraries_before_the_binaries_that_load_them(
    tmp_path: Path,
) -> None:
    files = _fake_package(tmp_path)

    plan = sign_standalone_package.plan_signing(tmp_path, is_macho=_fake_is_macho)

    depths = [len(path.parts) for path in plan.files]
    assert depths == sorted(depths, reverse=True)
    assert plan.files.index(files["site_packages"]) < plan.files.index(files["python"])


def test_plan_gives_only_the_interpreter_hardened_runtime_entitlements(
    tmp_path: Path,
) -> None:
    files = _fake_package(tmp_path)

    plan = sign_standalone_package.plan_signing(tmp_path, is_macho=_fake_is_macho)

    assert plan.entitlements == {
        files["python"]: sign_standalone_package.PYTHON_ENTITLEMENTS
    }
    entitlements = plistlib.loads(
        sign_standalone_package.PYTHON_ENTITLEMENTS.read_bytes()
    )
    assert entitlements["com.apple.security.cs.disable-library-validation"] is True
    assert (
        entitlements["com.apple.security.cs.allow-unsigned-executable-memory"] is True
    )


def test_sign_file_and_bundle_use_hardened_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        sign_standalone_package.subprocess,
        "run",
        lambda argv, check: calls.append(argv),
    )
    entitlements = tmp_path / "python.entitlements"

    sign_standalone_package.sign_file(
        tmp_path / "python3.12", "Developer ID", entitlements=entitlements
    )
    sign_standalone_package.sign_file(tmp_path / "libfoo.dylib", "Developer ID")
    sign_standalone_package.sign_bundle(
        tmp_path / "Openbase Services.app", "Developer ID"
    )

    for argv in calls:
        assert argv[:5] == [
            "codesign",
            "--force",
            "--timestamp",
            "--options",
            "runtime",
        ]
        assert argv[-3:-1] == ["--sign", "Developer ID"]
        # The identifier is derived from the file name / bundle Info.plist so
        # it matches what the desktop build's re-signing produces.
        assert "--identifier" not in argv and "--prefix" not in argv
    assert calls[0][calls[0].index("--entitlements") + 1] == str(entitlements)
    assert "--entitlements" not in calls[1]
    assert "--entitlements" not in calls[2]
    assert calls[2][-1] == str(tmp_path / "Openbase Services.app")


# --- package integration (real clang + codesign) -------------------------------


@needs_clang
def test_package_build_compiles_the_launcher_in_place_for_macos(tmp_path: Path) -> None:
    package_dir = tmp_path / "package"
    package_dir.mkdir()

    relative = build_standalone_package.stage_service_launcher(
        package_dir, None, version="2.0.0", target="aarch64-apple-darwin"
    )

    assert relative == LAUNCHER_RELATIVE_PATH
    executable = package_dir / relative
    assert executable.is_file() and os.access(executable, os.X_OK)
    info = plistlib.loads((executable.parents[1] / "Info.plist").read_bytes())
    assert info["CFBundleIdentifier"] == SERVICES_BUNDLE_ID
    assert info["CFBundleShortVersionString"] == "2.0.0"
    assert subprocess.run([str(executable), "true"]).returncode == 0


@needs_clang
def test_ad_hoc_package_signing_seals_the_launcher_bundle(
    launcher_app: Path, tmp_path: Path
) -> None:
    if shutil.which("codesign") is None:
        pytest.skip("codesign unavailable")
    package_dir = tmp_path / "package"
    bundle = package_dir / "libexec" / "Openbase Services.app"
    shutil.copytree(launcher_app, bundle, symlinks=True)
    # A bare Mach-O next to the bundle, like the Go binaries in bin/.
    loose = package_dir / "bin" / "openbase-tunneld"
    loose.parent.mkdir(parents=True)
    shutil.copy2(bundle / "Contents" / "MacOS" / "openbase-services", loose)

    build_standalone_package.ad_hoc_sign_macos_package(package_dir)

    subprocess.run(
        ["codesign", "--verify", "--strict", "--deep", str(bundle)], check=True
    )
    subprocess.run(["codesign", "--verify", "--strict", str(loose)], check=True)
    info = subprocess.run(
        ["codesign", "-dvv", str(bundle)], check=True, capture_output=True, text=True
    ).stderr
    assert f"Identifier={SERVICES_BUNDLE_ID}" in info
    assert "Signature=adhoc" in info
