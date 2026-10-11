#!/usr/bin/env python3
"""Build the Openbase Services launcher app bundle for the macOS runtime package.

The launcher (``cli/macos/service-launcher/openbase-services.c``) is the
launchd job process for every Openbase Coder service; its app bundle carries
the stable bundle identifier macOS keys TCC grants on. See
``dev-docs/MACOS_SERVICE_IDENTITY.md``.

Usage::

    build_service_launcher.py --version 1.2.3 --target aarch64-apple-darwin \
        --output "/path/to/Openbase Services.app"

The bundle is left unsigned here; ``build_standalone_package.py`` ad-hoc signs
it with the rest of the package and the release workflow re-signs everything
with the Developer ID identity (``sign_standalone_package.py``).
"""

from __future__ import annotations

import argparse
import plistlib
import shutil
import subprocess
from pathlib import Path

SOURCE_DIR = Path(__file__).resolve().parents[1] / "macos" / "service-launcher"
LAUNCHER_SOURCE = SOURCE_DIR / "openbase-services.c"
INFO_PLIST_TEMPLATE = SOURCE_DIR / "Info.plist"
EXECUTABLE_NAME = "openbase-services"
# Oldest macOS the launcher runs on; matches LSMinimumSystemVersion.
MINIMUM_MACOS_VERSION = "12.0"

_TARGET_ARCHITECTURES = {
    "aarch64-apple-darwin": "arm64",
    "x86_64-apple-darwin": "x86_64",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--version", required=True, help="Runtime package version")
    parser.add_argument(
        "--target", required=True, help="Package target, e.g. aarch64-apple-darwin"
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Path of the app bundle to create (e.g. 'Openbase Services.app')",
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    build_launcher_app(
        args.output.resolve(),
        version=args.version,
        target=args.target,
        force=args.force,
    )
    print(f"Built service launcher at {args.output.resolve()}")
    return 0


def architecture_for_target(target: str) -> str:
    try:
        return _TARGET_ARCHITECTURES[target]
    except KeyError as exc:
        raise RuntimeError(f"Unsupported launcher target: {target}") from exc


def compile_launcher(output: Path, *, architecture: str) -> None:
    """Compile the launcher executable with the system clang."""
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "xcrun",
            "clang",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-O2",
            "-arch",
            architecture,
            f"-mmacosx-version-min={MINIMUM_MACOS_VERSION}",
            str(LAUNCHER_SOURCE),
            "-o",
            str(output),
        ],
        check=True,
    )
    output.chmod(0o755)


def render_info_plist(*, version: str) -> bytes:
    """The bundle Info.plist with the release version stamped in."""
    info = plistlib.loads(INFO_PLIST_TEMPLATE.read_bytes())
    if info.get("CFBundleExecutable") != EXECUTABLE_NAME:
        raise RuntimeError(
            f"{INFO_PLIST_TEMPLATE} must name {EXECUTABLE_NAME} as CFBundleExecutable"
        )
    info["CFBundleShortVersionString"] = version
    info["CFBundleVersion"] = version
    return plistlib.dumps(info)


def build_launcher_app(
    app_dir: Path, *, version: str, target: str, force: bool = False
) -> Path:
    """Create ``app_dir`` (``<name>.app``) and return its main executable."""
    if app_dir.suffix != ".app":
        raise RuntimeError(f"Launcher bundle path must end in .app: {app_dir}")
    if app_dir.exists():
        if not force:
            raise RuntimeError(f"Launcher bundle already exists: {app_dir}")
        shutil.rmtree(app_dir)
    contents = app_dir / "Contents"
    executable = contents / "MacOS" / EXECUTABLE_NAME
    compile_launcher(executable, architecture=architecture_for_target(target))
    (contents / "Info.plist").write_bytes(render_info_plist(version=version))
    (contents / "PkgInfo").write_bytes(b"APPL????")
    return executable


if __name__ == "__main__":
    raise SystemExit(main())
