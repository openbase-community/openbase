#!/usr/bin/env python3
"""Build (and optionally sign) the Openbase Coder update manifest.

The manifest schema (``manifest_schema`` 1) is the durable contract defined in
the workspace-level AUTO_UPDATE.md. This script produces the
``update-manifest.json`` release asset consumed by ``openbase-coder
self-update``.

Signing: when the ``OPENBASE_UPDATE_SIGNING_KEY`` environment variable is set
(a base64-encoded raw 32-byte Ed25519 private key), an
``update-manifest.json.sig`` file is written next to the manifest containing
the base64-encoded raw Ed25519 signature of the exact manifest bytes. When the
variable is unset, signing is skipped silently. Signing requires the
``cryptography`` package (a dependency of the CLI, so the bundled runtime
Python always has it).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path

MANIFEST_SCHEMA = 1
SIGNING_KEY_ENV = "OPENBASE_UPDATE_SIGNING_KEY"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build the update-manifest.json release asset."
    )
    parser.add_argument("--version", required=True, help="Release version, e.g. 0.2.0")
    parser.add_argument(
        "--channel", default="stable", choices=("stable", "beta", "staging")
    )
    parser.add_argument(
        "--target", required=True, help="Target triple, e.g. aarch64-apple-darwin"
    )
    parser.add_argument(
        "--tarball",
        type=Path,
        required=True,
        help="Path to the built runtime package tarball for --target.",
    )
    parser.add_argument(
        "--url-base",
        required=True,
        help=(
            "Base URL the tarball will be downloaded from, e.g. "
            "https://github.com/openbase-community/openbase/releases/download/v0.2.0/"
        ),
    )
    parser.add_argument(
        "--repo-shas",
        help="JSON object mapping repo names to the commit SHAs baked into the package.",
    )
    parser.add_argument("--min-supported-version", default="0.1.0")
    parser.add_argument(
        "--layout-version",
        type=int,
        help="Package layout version (defaults to --package-metadata layoutVersion, else 1).",
    )
    parser.add_argument(
        "--python-version",
        help="Bundled Python version, e.g. 3.12.8 (defaults to --package-metadata pythonVersion).",
    )
    parser.add_argument(
        "--package-metadata",
        type=Path,
        help="openbase-coder-package.json to read pythonVersion/layoutVersion/repo_shas from.",
    )
    parser.add_argument(
        "--merge-existing",
        type=Path,
        help=(
            "Existing update-manifest.json to merge with, so another target's "
            "job can add its entry to the targets map."
        ),
    )
    parser.add_argument("--output", type=Path, default=Path("update-manifest.json"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    package_metadata = load_package_metadata(args.package_metadata)

    python_version = args.python_version or package_metadata.get("pythonVersion")
    if not python_version:
        raise SystemExit("Provide --python-version or --package-metadata.")
    layout_version = first_non_none(
        args.layout_version, package_metadata.get("layoutVersion"), 1
    )
    repo_shas = (
        parse_repo_shas(args.repo_shas)
        if args.repo_shas is not None
        else package_metadata.get("repo_shas", {})
    )

    manifest = {
        "manifest_schema": MANIFEST_SCHEMA,
        "channel": args.channel,
        "version": args.version,
        "layout_version": layout_version,
        "min_supported_version": args.min_supported_version,
        "python_version": python_version,
        "targets": {args.target: target_entry(args.tarball, args.url_base)},
        "repo_shas": repo_shas,
    }
    if args.merge_existing:
        manifest = merge_manifests(load_manifest(args.merge_existing), manifest)

    manifest_bytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(manifest_bytes)
    print(f"Wrote manifest to {args.output}")
    maybe_sign(manifest_bytes, args.output)
    return 0


def target_entry(tarball: Path, url_base: str) -> dict[str, object]:
    if not tarball.is_file():
        raise SystemExit(f"Tarball not found: {tarball}")
    digest = hashlib.sha256()
    with tarball.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "url": url_base.rstrip("/") + "/" + tarball.name,
        "sha256": digest.hexdigest(),
        "size": tarball.stat().st_size,
    }


def merge_manifests(existing: dict, new: dict) -> dict:
    for key in (
        "manifest_schema",
        "channel",
        "version",
        "layout_version",
        "min_supported_version",
        "python_version",
    ):
        if existing.get(key) != new[key]:
            raise SystemExit(
                f"Cannot merge manifests: {key!r} differs "
                f"({existing.get(key)!r} != {new[key]!r})"
            )
    merged = dict(new)
    merged["targets"] = {**existing.get("targets", {}), **new["targets"]}
    merged["repo_shas"] = {**existing.get("repo_shas", {}), **new["repo_shas"]}
    return merged


def load_manifest(path: Path) -> dict:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise SystemExit(f"Existing manifest is not a JSON object: {path}")
    if manifest.get("manifest_schema") != MANIFEST_SCHEMA:
        raise SystemExit(
            f"Existing manifest has unsupported manifest_schema "
            f"{manifest.get('manifest_schema')!r}: {path}"
        )
    return manifest


def load_package_metadata(path: Path | None) -> dict:
    if path is None:
        return {}
    metadata = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise SystemExit(f"Package metadata is not a JSON object: {path}")
    return metadata


def parse_repo_shas(raw: str) -> dict[str, str]:
    value = json.loads(raw)
    if not isinstance(value, dict) or not all(
        isinstance(key, str) and isinstance(sha, str) for key, sha in value.items()
    ):
        raise SystemExit("--repo-shas must be a JSON object mapping repo name to SHA")
    return value


def first_non_none(*values):
    for value in values:
        if value is not None:
            return value
    return None


def maybe_sign(manifest_bytes: bytes, output: Path) -> None:
    encoded_key = os.environ.get(SIGNING_KEY_ENV, "").strip()
    if not encoded_key:
        return
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    raw_key = base64.b64decode(encoded_key)
    if len(raw_key) != 32:
        raise SystemExit(
            f"{SIGNING_KEY_ENV} must be a base64-encoded raw 32-byte Ed25519 private key"
        )
    signature = Ed25519PrivateKey.from_private_bytes(raw_key).sign(manifest_bytes)
    sig_path = output.with_name(output.name + ".sig")
    sig_path.write_text(
        base64.b64encode(signature).decode("ascii") + "\n", encoding="utf-8"
    )
    print(f"Wrote signature to {sig_path}")


if __name__ == "__main__":
    raise SystemExit(main())
