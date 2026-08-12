#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_ROOT = REPO_ROOT / "cli"
CONSOLE_ROOT = REPO_ROOT / "console"
INSTRUCTIONS_ROOT = REPO_ROOT / "instructions"
SKILLS_ROOT = REPO_ROOT / "skills"
SUPER_AGENTS_ROOT = REPO_ROOT / "super-agents"
METADATA_FILENAME = "openbase-coder-package.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an Openbase Coder standalone package."
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--target", default=default_target())
    parser.add_argument("--channel", default="stable")
    parser.add_argument(
        "--repo-shas",
        default="{}",
        help="JSON object mapping repo names to the commit SHAs baked into the package.",
    )
    parser.add_argument(
        "--python",
        type=Path,
        default=default_runtime_python(),
        help="Python executable to bundle. Python 3.12 is preferred for local audio.",
    )
    parser.add_argument("--package-dir", type=Path)
    parser.add_argument("--archive-output", type=Path)
    parser.add_argument("--livekit-server-bin", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--skip-console-build", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package_dir = (
        args.package_dir.resolve()
        if args.package_dir
        else Path(tempfile.mkdtemp(prefix="openbase-coder-package-")).resolve()
    )
    prepare_dir(package_dir, force=args.force)

    python_dir = package_dir / "python"
    create_runtime_python(python_dir, args.python)
    install_cli_package(python_dir)
    rewrite_bin_shebangs(python_dir)
    relocate_macos_python(python_dir)
    stage_bin(
        package_dir,
        python_dir,
        args.livekit_server_bin.resolve(),
    )
    stage_console(package_dir, skip_build=args.skip_console_build)
    stage_optional_tree(INSTRUCTIONS_ROOT, package_dir / "instructions")
    stage_optional_tree(SKILLS_ROOT / "skills", package_dir / "skills")
    write_metadata(
        package_dir,
        version=args.version,
        target=args.target,
        channel=args.channel,
        repo_shas=parse_repo_shas(args.repo_shas),
    )
    ad_hoc_sign_macos_package(package_dir)
    validate_package(package_dir, args.version)

    if args.archive_output:
        write_archive(package_dir, args.archive_output.resolve(), force=args.force)
        print(f"Built archive at {args.archive_output.resolve()}")

    print(f"Built package at {package_dir}")
    return 0


def default_target() -> str:
    system = platform.system()
    machine = platform.machine().lower()
    if system == "Darwin":
        if machine in {"arm64", "aarch64"}:
            return "aarch64-apple-darwin"
        if machine in {"x86_64", "amd64"}:
            return "x86_64-apple-darwin"
    raise SystemExit(f"Unsupported standalone package host: {system} {machine}")


def default_runtime_python() -> Path:
    if value := _uv_managed_python("3.12"):
        return value
    if value := shutil.which("python3.12"):
        return Path(value)
    return Path(sys.executable)


def prepare_dir(path: Path, *, force: bool) -> None:
    if path.exists() and any(path.iterdir()):
        if not force:
            raise RuntimeError(f"Package directory is not empty: {path}")
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def create_runtime_python(python_dir: Path, python_executable: Path) -> None:
    python_root = python_executable.resolve().parent.parent
    if not (python_root / "bin").is_dir():
        raise RuntimeError(
            f"Python executable is not in a Python tree: {python_executable}"
        )

    shutil.copytree(
        python_root,
        python_dir,
        symlinks=True,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    runtime_python(python_dir).chmod(
        runtime_python(python_dir).stat().st_mode | stat.S_IXUSR
    )
    subprocess.run(
        [str(runtime_python(python_dir)), "-m", "pip", "install", "--upgrade", "pip"],
        check=True,
        env=_runtime_pip_env(),
    )


def install_cli_package(python_dir: Path) -> None:
    # super-agents rides sibling-branch HEADs like every other repo in
    # repo_shas: install it from source first so pip sees the requirement
    # satisfied instead of resolving super-agents from PyPI.
    if not SUPER_AGENTS_ROOT.exists():
        raise SystemExit(
            f"super-agents checkout missing at {SUPER_AGENTS_ROOT}; "
            "a PyPI fallback would silently unpin the packaged snapshot."
        )
    subprocess.run(
        [
            str(runtime_python(python_dir)),
            "-m",
            "pip",
            "install",
            f"{SUPER_AGENTS_ROOT}[claude]",
        ],
        check=True,
        env=_runtime_pip_env(),
    )
    subprocess.run(
        [
            str(runtime_python(python_dir)),
            "-m",
            "pip",
            "install",
            str(CLI_ROOT),
        ],
        check=True,
        env=_runtime_pip_env(),
    )
    _verify_source_super_agents(python_dir)


def _verify_source_super_agents(python_dir: Path) -> None:
    # If the cli's super-agents version floor ever exceeds the sibling
    # checkout's static version, the cli install above would replace the
    # source build with a PyPI wheel. Path installs record a direct_url.json;
    # require it.
    check = (
        "import importlib.metadata as m, sys; "
        "files = m.files('super-agents') or []; "
        "sys.exit(0 if any(f.name == 'direct_url.json' for f in files) else 1)"
    )
    result = subprocess.run(
        [str(runtime_python(python_dir)), "-c", check],
        env=_runtime_pip_env(),
    )
    if result.returncode != 0:
        raise SystemExit(
            "Packaged super-agents came from PyPI, not the sibling checkout; "
            "bump the version in super-agents/pyproject.toml to satisfy the "
            "cli's floor, or lower the floor."
        )


def rewrite_bin_shebangs(python_dir: Path) -> None:
    # pip writes each entry-point script with an absolute shebang pointing at
    # the build machine's interpreter, which breaks once the package is
    # installed anywhere else. Rewrite them to resolve the bundled interpreter
    # relative to the script itself.
    interpreter_name = runtime_python(python_dir).resolve().name
    header = (
        "#!/bin/sh\n"
        f'\'\'\'exec\' "$(dirname -- "$0")/{interpreter_name}" "$0" "$@"\n'
        "' '''\n"
    ).encode("utf-8")
    for script in sorted((python_dir / "bin").iterdir()):
        if script.is_symlink() or not script.is_file():
            continue
        body = _python_script_body(script.read_bytes())
        if body is not None:
            script.write_bytes(header + body)


def _python_script_body(raw: bytes) -> bytes | None:
    """Return the code after the shebang header if it needs a rewrite."""
    first_line, _, rest = raw.partition(b"\n")
    if not first_line.startswith(b"#!"):
        return None
    if first_line.strip() == b"#!/bin/sh":
        # distlib's spaces-in-path form: a three-line /bin/sh trampoline.
        exec_line, _, rest = rest.partition(b"\n")
        close_line, _, rest = rest.partition(b"\n")
        if (
            exec_line.startswith(b"'''exec'")
            and close_line.strip() == b"' '''"
            and not _is_relocatable_trampoline(
                exec_line.decode("utf-8", errors="replace")
            )
        ):
            return rest
        return None
    return rest if b"python" in first_line else None


def _is_relocatable_trampoline(exec_line: str) -> bool:
    return "$(dirname -- " in exec_line


def relocate_macos_python(python_dir: Path) -> None:
    if platform.system() != "Darwin" or not shutil.which("install_name_tool"):
        return

    lib_dir = python_dir / "lib"
    libpython_paths = tuple(sorted(lib_dir.glob("libpython*.dylib")))
    packaged_libraries_by_name = {
        path.name: path for path in _packaged_native_libraries(python_dir)
    }

    for native_library_path in packaged_libraries_by_name.values():
        _relocate_macho_install_id(native_library_path)

    libpython_names = {path.name for path in libpython_paths}
    for macho_path in _macho_files(python_dir):
        changes: list[str] = []
        for linked in _macho_linked_libraries(macho_path):
            linked_name = Path(linked).name
            if not _is_relocatable_link(linked):
                continue
            packaged_dylib = packaged_libraries_by_name.get(linked_name)
            if packaged_dylib is None and linked_name in libpython_names:
                packaged_dylib = lib_dir / linked_name
            if packaged_dylib is None:
                continue
            replacement = _dylib_reference_for(macho_path, packaged_dylib)
            changes.extend(["-change", linked, replacement])
        if changes:
            subprocess.run(
                ["install_name_tool", *changes, str(macho_path)],
                check=True,
            )


def stage_bin(
    package_dir: Path,
    python_dir: Path,
    livekit_server_bin: Path,
) -> None:
    if not livekit_server_bin.is_file():
        raise RuntimeError(f"LiveKit binary not found: {livekit_server_bin}")
    bin_dir = package_dir / "bin"
    bin_dir.mkdir()
    launcher = bin_dir / "openbase-coder"
    launcher.write_text(
        "#!/bin/sh\n"
        'ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"\n'
        'export OPENBASE_CODER_PACKAGE_DIR="$ROOT"\n'
        'exec "$ROOT/python/bin/python" -m openbase_coder_cli.cli "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    shutil.copy2(livekit_server_bin, bin_dir / "livekit-server")
    (bin_dir / "livekit-server").chmod(0o755)


def stage_console(package_dir: Path, *, skip_build: bool) -> None:
    if not skip_build:
        install_args = (
            ["install", "--frozen-lockfile"]
            if (REPO_ROOT / "pnpm-lock.yaml").is_file()
            else ["install", "--no-frozen-lockfile", "--shamefully-hoist"]
        )
        for package_name in (
            "console",
            "coder-react",
            "multi-react",
            "boilersync-react",
        ):
            node_modules_dir = REPO_ROOT / package_name / "node_modules"
            if node_modules_dir.is_symlink() or (
                node_modules_dir.exists() and not node_modules_dir.is_dir()
            ):
                node_modules_dir.unlink()
            node_modules_dir.mkdir(exist_ok=True)
        subprocess.run(["pnpm", *install_args], cwd=REPO_ROOT, check=True)
        subprocess.run(["pnpm", "--dir", str(CONSOLE_ROOT), "run", "build"], check=True)
    dist = CONSOLE_ROOT / "dist"
    if not dist.is_dir():
        raise RuntimeError("Console dist not found; build the console first.")
    shutil.copytree(dist, package_dir / "console")


def stage_optional_tree(source: Path, dest: Path) -> None:
    if source.is_dir():
        shutil.copytree(source, dest)


def parse_repo_shas(raw: str) -> dict[str, str]:
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(sha, str) for key, sha in value.items()
    ):
        raise SystemExit("--repo-shas must be a JSON object mapping repo name to SHA")
    return value


def write_metadata(
    package_dir: Path,
    *,
    version: str,
    target: str,
    channel: str,
    repo_shas: dict[str, str],
) -> None:
    metadata = {
        "layoutVersion": 1,
        "version": version,
        "target": target,
        "channel": channel,
        "entrypoint": "bin/openbase-coder",
        "python": "python/bin/python",
        "pythonVersion": package_python_version(package_dir),
        "console": "console",
        "livekit": "bin/livekit-server",
        "repo_shas": repo_shas,
    }
    (package_dir / METADATA_FILENAME).write_text(
        json.dumps(metadata, indent=2) + "\n",
        encoding="utf-8",
    )


def validate_package(package_dir: Path, version: str) -> None:
    required = [
        package_dir / METADATA_FILENAME,
        package_dir / "bin" / "openbase-coder",
        package_dir / "bin" / "livekit-server",
        package_dir / "python" / "bin" / "python",
        package_dir / "console" / "index.html",
    ]
    for path in required:
        if not path.exists():
            raise RuntimeError(f"Package validation failed; missing {path}")
    _validate_no_external_python_links(package_dir)
    _validate_no_host_macos_library_links(package_dir)
    _validate_relocatable_bin_shebangs(package_dir)
    _validate_no_build_path_shebangs(package_dir)
    subprocess.run(
        [str(package_dir / "python" / "bin" / "pip"), "--version"],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        [str(package_dir / "bin" / "openbase-coder"), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    reported = result.stdout.strip()
    print(reported)
    if version not in reported:
        raise SystemExit(
            f"Packaged CLI reports {reported!r}, expected version {version!r}: "
            "the release version stamp did not reach the build."
        )


def write_archive(package_dir: Path, archive_path: Path, *, force: bool) -> None:
    if archive_path.exists():
        if not force:
            raise RuntimeError(f"Archive already exists: {archive_path}")
        archive_path.unlink()
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, "w:gz") as archive:
        for child in sorted(package_dir.iterdir(), key=lambda item: item.name):
            archive.add(child, arcname=child.name)


def ad_hoc_sign_macos_package(package_dir: Path) -> None:
    if platform.system() != "Darwin" or not shutil.which("codesign"):
        return
    for path in sorted(
        _macho_files(package_dir), key=lambda item: len(item.parts), reverse=True
    ):
        subprocess.run(
            ["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )


def runtime_python(python_dir: Path) -> Path:
    return python_dir / "bin" / "python"


def _runtime_pip_env() -> dict[str, str]:
    return {**os.environ, "PIP_BREAK_SYSTEM_PACKAGES": "1"}


def _uv_managed_python(version: str) -> Path | None:
    uv = shutil.which("uv")
    if not uv:
        return None
    result = subprocess.run(
        [uv, "python", "find", "--managed-python", version],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    path = Path(result.stdout.strip())
    return path if path.is_file() else None


def _validate_no_build_path_shebangs(package_dir: Path) -> None:
    """Fail the build if any script hardcodes the build-time package directory.

    ``bin/openbase-coder`` is a hand-written relocatable launcher, so the CLI
    smoke test in validate_package() passes even when every pip-generated
    console script in python/bin is broken. Check them directly.
    """
    for path in sorted((package_dir / "python" / "bin").iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if not text.startswith("#!"):
            continue
        # Check the exec line too: the sh-wrapper form pip emits for paths
        # containing spaces keeps the absolute interpreter path on line 2.
        for line in text.split("\n", 2)[:2]:
            if str(package_dir) in line:
                raise RuntimeError(
                    "Package validation failed; script has a non-relocatable "
                    f"build-path reference: {path} -> {line.strip()}"
                )


def _validate_no_external_python_links(package_dir: Path) -> None:
    python_dir = package_dir / "python"
    for path in python_dir.rglob("*"):
        if not path.is_symlink():
            continue
        resolved = path.resolve()
        try:
            resolved.relative_to(python_dir.resolve())
        except ValueError as exc:
            raise RuntimeError(
                "Package validation failed; bundled Python contains external "
                f"symlink {path} -> {resolved}"
            ) from exc


def _validate_relocatable_bin_shebangs(package_dir: Path) -> None:
    # Tripwire for the 0.10.0 regression: a script shebang naming the build
    # machine's interpreter dies on every user's machine.
    for script in sorted((package_dir / "python" / "bin").iterdir()):
        if script.is_symlink() or not script.is_file():
            continue
        with script.open("rb") as handle:
            header = handle.read(1024)
        if not header.startswith(b"#!"):
            continue
        lines = header.decode("utf-8", errors="replace").splitlines()
        if "python" in lines[0]:
            raise RuntimeError(
                "Package validation failed; script shebang points at the "
                f"build machine's interpreter: {script} ({lines[0]})"
            )
        if (
            len(lines) > 1
            and lines[1].startswith("'''exec'")
            and not _is_relocatable_trampoline(lines[1])
        ):
            raise RuntimeError(
                "Package validation failed; script trampoline is not "
                f"relocatable: {script} ({lines[1]})"
            )


def _validate_no_host_macos_library_links(package_dir: Path) -> None:
    if platform.system() != "Darwin" or not shutil.which("otool"):
        return

    for path in _macho_files(package_dir):
        result = subprocess.run(
            ["otool", "-L", str(path)],
            check=True,
            capture_output=True,
            text=True,
        )
        for raw_line in result.stdout.splitlines()[1:]:
            if not raw_line.startswith("\t"):
                continue
            linked = raw_line.strip().split(" ", 1)[0]
            if _is_relocatable_link(linked):
                raise RuntimeError(
                    "Package validation failed; Mach-O file links to build-host "
                    f"library {linked}: {path}"
                )


def _macho_files(package_dir: Path):
    for path in package_dir.rglob("*"):
        if not _could_be_macho(path):
            continue
        result = subprocess.run(
            ["file", "-b", str(path)],
            check=True,
            capture_output=True,
            errors="replace",
            text=True,
        )
        if "Mach-O" in result.stdout:
            yield path


def _could_be_macho(path: Path) -> bool:
    if not path.is_file() or path.is_symlink():
        return False
    if os.access(path, os.X_OK):
        return True
    if path.suffix in {".dylib", ".so", ".bundle"}:
        return True
    return any(suffix.endswith(".so") for suffix in path.suffixes)


def _macho_install_id(path: Path) -> str | None:
    result = subprocess.run(
        ["otool", "-D", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return lines[1] if len(lines) > 1 else None


def _macho_linked_libraries(path: Path) -> list[str]:
    result = subprocess.run(
        ["otool", "-L", str(path)],
        check=True,
        capture_output=True,
        text=True,
    )
    return [
        raw_line.strip().split(" ", 1)[0]
        for raw_line in result.stdout.splitlines()[1:]
        if raw_line.startswith("\t") and raw_line.strip()
    ]


def _is_relocatable_link(value: str) -> bool:
    if not value.startswith("/"):
        return False
    return not value.startswith(("/usr/lib/", "/System/Library/"))


def _packaged_dylibs(python_dir: Path) -> tuple[Path, ...]:
    return tuple(sorted(path for path in python_dir.rglob("*.dylib") if path.is_file()))


def _packaged_native_libraries(python_dir: Path) -> tuple[Path, ...]:
    paths = set(_packaged_dylibs(python_dir))
    for path in python_dir.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix in {".so", ".bundle"} or any(
            suffix.endswith(".so") for suffix in path.suffixes
        ):
            paths.add(path)
    return tuple(sorted(paths))


def _relocate_macho_install_id(path: Path) -> None:
    old_id = _macho_install_id(path)
    if not old_id or not _is_relocatable_link(old_id):
        return
    subprocess.run(
        ["install_name_tool", "-id", f"@rpath/{path.name}", str(path)],
        check=True,
    )


def _dylib_reference_for(macho_path: Path, dylib_path: Path) -> str:
    if macho_path.parent.name == "bin":
        return f"@executable_path/../lib/{dylib_path.name}"

    relative = os.path.relpath(dylib_path, start=macho_path.parent)
    return f"@loader_path/{relative}"


def package_python_version(package_dir: Path) -> str:
    result = subprocess.run(
        [
            str(package_dir / "python" / "bin" / "python"),
            "-c",
            "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


if __name__ == "__main__":
    raise SystemExit(main())
