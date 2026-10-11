#!/usr/bin/env python3
"""Code sign the Mach-O files and app bundles of an Openbase Coder package.

Run by the release workflow with the Developer ID Application identity so
every macOS runtime release carries the same code identity: TCC keys
Desktop/Documents/Local Network grants on a binary's designated requirement,
which for a Developer ID signature is ``identifier + Team ID`` (stable across
releases) and for an ad-hoc signature is its cdhash (new on every build).
The Openbase Services launcher bundle is what the launchd jobs run through,
so its identifier ``cloud.openbase.coder.services`` is the identity users
grant; see dev-docs/MACOS_SERVICE_IDENTITY.md.

Signing order: bare Mach-O files deepest-first (nested libraries before the
executables that load them), skipping anything inside an app bundle, then
each app bundle as a unit (which signs its main executable and seals its
Info.plist). The bundled Python interpreter gets hardened-runtime
entitlements (cli/macos/python-runtime.entitlements) because it loads
extension modules from plugin sites we do not sign.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

PYTHON_ENTITLEMENTS = (
    Path(__file__).resolve().parents[1] / "macos" / "python-runtime.entitlements"
)
_PYTHON_INTERPRETER_NAME = re.compile(r"^python3(\.\d+)?$")


@dataclass(frozen=True)
class SigningPlan:
    """What ``sign`` will sign, in order."""

    files: tuple[Path, ...]
    bundles: tuple[Path, ...]
    entitlements: dict[Path, Path]

    def covers(self, path: Path) -> bool:
        """Whether ``path`` is signed directly or sealed inside a planned bundle."""
        return path in self.files or any(
            path == bundle or bundle in path.parents for bundle in self.bundles
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Code sign Mach-O files and app bundles in an Openbase Coder package."
    )
    parser.add_argument("--package-dir", type=Path, required=True)
    parser.add_argument("--identity", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the signing plan without calling codesign.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package_dir = args.package_dir.resolve()
    if not package_dir.is_dir():
        raise RuntimeError(f"Package directory not found: {package_dir}")

    plan = plan_signing(package_dir)
    if not plan.files and not plan.bundles:
        print("No Mach-O files found to sign.")
        return 0

    for path in plan.files:
        entitlements = plan.entitlements.get(path)
        suffix = f" (entitlements: {entitlements.name})" if entitlements else ""
        if args.dry_run:
            print(f"{path}{suffix}")
            continue
        sign_file(path, args.identity, entitlements=entitlements)
        print(f"Signed {path}{suffix}")
    for bundle in plan.bundles:
        if args.dry_run:
            print(f"{bundle} (bundle)")
            continue
        sign_bundle(bundle, args.identity)
        print(f"Signed bundle {bundle}")
    return 0


def plan_signing(package_dir: Path, *, is_macho=None) -> SigningPlan:
    """Build the signing plan for ``package_dir``.

    ``is_macho`` defaults to a ``file(1)`` probe; tests inject a predicate.
    """
    probe = is_macho or is_macho_file
    bundles = app_bundles(package_dir)
    files = tuple(
        path
        for path in macho_candidates(package_dir)
        if not any(bundle in path.parents for bundle in bundles) and probe(path)
    )
    entitlements = {
        path: PYTHON_ENTITLEMENTS for path in files if is_python_interpreter(path)
    }
    return SigningPlan(files=files, bundles=bundles, entitlements=entitlements)


def app_bundles(package_dir: Path) -> tuple[Path, ...]:
    return tuple(
        sorted(
            path
            for path in package_dir.rglob("*.app")
            if path.is_dir() and (path / "Contents" / "Info.plist").is_file()
        )
    )


def macho_candidates(package_dir: Path) -> list[Path]:
    """Regular files that may be Mach-O, deepest paths first."""
    return sorted(
        (path for path in package_dir.rglob("*") if could_be_macho(path)),
        key=lambda path: (-len(path.parts), str(path)),
    )


def could_be_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    if os.access(path, os.X_OK):
        return True
    if path.suffix in {".dylib", ".so", ".bundle"}:
        return True
    return any(suffix.endswith(".so") for suffix in path.suffixes)


def is_macho_file(path: Path) -> bool:
    result = subprocess.run(
        ["file", "-b", str(path)],
        check=True,
        capture_output=True,
        errors="replace",
        text=True,
    )
    return "Mach-O" in result.stdout


def is_python_interpreter(path: Path) -> bool:
    """The bundled CPython executable(s): ``python/bin/python3[.X]``."""
    return (
        path.parent.name == "bin"
        and path.parent.parent.name == "python"
        and _PYTHON_INTERPRETER_NAME.match(path.name) is not None
    )


def _codesign(identity: str, *extra: str, target: Path) -> None:
    subprocess.run(
        [
            "codesign",
            "--force",
            "--timestamp",
            "--options",
            "runtime",
            *extra,
            "--sign",
            identity,
            str(target),
        ],
        check=True,
    )


def sign_file(path: Path, identity: str, *, entitlements: Path | None = None) -> None:
    extra = ("--entitlements", str(entitlements)) if entitlements else ()
    _codesign(identity, *extra, target=path)


def sign_bundle(bundle: Path, identity: str) -> None:
    _codesign(identity, target=bundle)


if __name__ == "__main__":
    raise SystemExit(main())
