#!/usr/bin/env python3
"""Download and verify the pinned Openbase Sync engine binaries.

The engine (openbase-syncd, openbase-sync, edge) is closed source and ships
as prebuilt, checksummed binaries; this script fetches the version pinned in
cli/sync_engine.json for a target, verifies its sha256, and extracts it.

    fetch_sync_engine.py --target darwin-arm64 --out DIR
    fetch_sync_engine.py --target aarch64-apple-darwin --out DIR   # Rust-style triples accepted
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import tarfile
import urllib.request
from pathlib import Path

PIN = Path(__file__).resolve().parent.parent / "sync_engine.json"
BINARIES = ("openbase-syncd", "openbase-sync", "edge")
TRIPLES = {
    "aarch64-apple-darwin": "darwin-arm64",
    "x86_64-apple-darwin": "darwin-amd64",
    "aarch64-unknown-linux-gnu": "linux-arm64",
    "x86_64-unknown-linux-gnu": "linux-amd64",
    "arm64": "linux-arm64",
    "amd64": "linux-amd64",
}


def normalize_target(target: str) -> str:
    return TRIPLES.get(target, target)


def fetch(target: str, out: Path, pin_path: Path = PIN) -> list[Path]:
    pin = json.loads(pin_path.read_text(encoding="utf-8"))
    target = normalize_target(target)
    expected = pin["sha256"].get(target)
    if not expected:
        raise SystemExit(f"no pinned sync engine for target {target!r}")
    url = (
        f"{pin['base_url'].rstrip('/')}/{pin['version']}/openbase-sync-{target}.tar.gz"
    )
    with urllib.request.urlopen(url, timeout=120) as resp:  # noqa: S310 - pinned https URL
        data = resp.read()
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise SystemExit(
            f"sync engine checksum mismatch for {target}: {actual} != {expected}"
        )
    out.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for name in BINARIES:
            member = tar.getmember(name)
            src = tar.extractfile(member)
            if src is None:
                raise SystemExit(f"{name} missing from the engine archive")
            dest = out / name
            dest.write_bytes(src.read())
            dest.chmod(0o755)
            written.append(dest)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument(
        "--pin", type=Path, default=PIN, help="pin file (default: cli/sync_engine.json)"
    )
    args = parser.parse_args()
    for path in fetch(args.target, args.out, pin_path=args.pin):
        print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
