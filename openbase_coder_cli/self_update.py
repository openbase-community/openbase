"""Self-update for standalone Openbase Coder installs.

The full contract (feed, atomicity, rollback, quiescing, channels, signing)
lives in the workspace ``AUTO_UPDATE.md`` guide — keep the two in sync.
"""

from __future__ import annotations

import asyncio
import base64
import fcntl
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tarfile
import tempfile
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from packaging.version import InvalidVersion, Version

from openbase_coder_cli._version import __version__
from openbase_coder_cli.backend_binaries import refresh_openbase_bin_codex
from openbase_coder_cli.paths import (
    DEFAULT_LOG_DIR,
    OPENBASE_BASE_DIR,
    STANDALONE_CURRENT_DIR,
    STANDALONE_PACKAGES_DIR,
    STANDALONE_RELEASES_DIR,
)
from openbase_coder_cli.runtime import (
    PACKAGE_METADATA_FILENAME,
    RuntimePackage,
    current_runtime_package,
)

logger = logging.getLogger(__name__)

MANIFEST_SCHEMA = 1
SUPPORTED_LAYOUT_VERSION = 1
KEEP_RELEASES = 2
RELEASE_REPO = "openbase-community/openbase"
STABLE_MANIFEST_URL = (
    f"https://github.com/{RELEASE_REPO}/releases/latest/download/update-manifest.json"
)
RELEASES_API_URL = f"https://api.github.com/repos/{RELEASE_REPO}/releases?per_page=15"
MANIFEST_ASSET_NAME = "update-manifest.json"
MANIFEST_SIGNATURE_ASSET_NAME = "update-manifest.json.sig"
# Base64 raw 32-byte Ed25519 public key pinning the release signing key;
# manifest signature verification is mandatory (see AUTO_UPDATE.md).
UPDATE_MANIFEST_PUBLIC_KEY_B64 = "5Si9SNGu++/mq0OOy3LAO1jdSRRPfBuy6D1i0MCJ+n4="
STANDALONE_PREVIOUS_DIR = STANDALONE_PACKAGES_DIR / "previous"
UPDATE_CHECK_CACHE_PATH = OPENBASE_BASE_DIR / "update-check.json"
DOWNLOAD_TIMEOUT_SECONDS = 30
AUTO_UPDATE_ENV_KEY = "OPENBASE_CODER_AUTO_UPDATE"
SELF_UPDATE_LOG_PATH = DEFAULT_LOG_DIR / "self-update.log"


class SelfUpdateError(RuntimeError):
    pass


@dataclass(frozen=True)
class UpdateCheck:
    current_version: str
    latest_version: str | None
    channel: str
    update_available: bool
    update_required: bool
    detail: str = ""


@dataclass(frozen=True)
class SelfUpdateResult:
    status: str  # updated | up-to-date | deferred | blocked | rolled-back
    from_version: str
    to_version: str | None
    detail: str = ""


def installed_channel(runtime_package: RuntimePackage | None = None) -> str:
    package = runtime_package or current_runtime_package()
    if package is None:
        return "stable"
    metadata = _read_package_metadata(package.root)
    channel = str(metadata.get("channel", "")).strip()
    return channel or "stable"


def version_info() -> dict:
    """Static version facts plus cached update flags (never touches network)."""
    package = current_runtime_package()
    cache = _read_update_check_cache()
    info: dict = {
        "cli": __version__,
        "standalone": package is not None,
        "channel": installed_channel(package),
        "layout_version": SUPPORTED_LAYOUT_VERSION,
    }
    if package is not None:
        info["target"] = package.target
        info["package_version"] = package.version
    info["update_available"] = bool(cache.get("update_available"))
    info["update_required"] = bool(cache.get("update_required"))
    if cache.get("latest_version"):
        info["latest_version"] = cache["latest_version"]
    return info


def check_for_update() -> UpdateCheck:
    """Fetch the manifest and compare versions; caches the result for status."""
    package = current_runtime_package()
    channel = installed_channel(package)
    if package is None:
        return UpdateCheck(
            current_version=__version__,
            latest_version=None,
            channel=channel,
            update_available=False,
            update_required=False,
            detail="Development workspace installs are git-managed; no auto-update.",
        )

    manifest = _fetch_manifest(channel)
    current = _parse_version(package.version or __version__)
    latest = _parse_version(str(manifest.get("version", "")))
    update_available = latest is not None and current is not None and latest > current
    update_required = _below_minimum(current, manifest)
    check = UpdateCheck(
        current_version=str(current or package.version or __version__),
        latest_version=str(latest) if latest else None,
        channel=channel,
        update_available=update_available,
        update_required=update_required,
    )
    _write_update_check_cache(check)
    return check


def auto_update_enabled() -> bool:
    """Automatic updates apply by default on standalone installs; opt out with
    OPENBASE_CODER_AUTO_UPDATE=0 (in the environment or ~/.openbase/.env)."""
    value = os.environ.get(AUTO_UPDATE_ENV_KEY, "").strip().lower()
    return value not in {"0", "false", "no", "off"}


def spawn_detached_self_update(*, force: bool = False) -> None:
    """Launch `openbase-coder self-update` as a detached process.

    Detaching matters because applying an update reinstalls the launchd/systemd
    services — including whichever service invoked this — and an in-process
    update would be killed by its own service restart mid-flip. Output goes to
    the self-update log so failures stay diagnosable.
    """
    launcher = STANDALONE_CURRENT_DIR / "bin" / "openbase-coder"
    if not launcher.is_file():
        raise SelfUpdateError(f"No standalone launcher at {launcher}.")
    SELF_UPDATE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    args = [str(launcher), "self-update"]
    if force:
        args.append("--force")
    with SELF_UPDATE_LOG_PATH.open("ab") as log_handle:
        subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            start_new_session=True,
        )


def run_self_update(*, force: bool = False, report=print) -> SelfUpdateResult:
    package = current_runtime_package()
    if package is None:
        raise SelfUpdateError(
            "self-update only applies to standalone installs; this CLI runs "
            "from a development workspace (git-managed)."
        )

    # Serialize concurrent invocations (desktop-triggered, manual, scripted):
    # two updaters racing the extract/flip would corrupt the release layout.
    STANDALONE_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STANDALONE_PACKAGES_DIR / ".self-update.lock"
    lock_handle = lock_path.open("w")
    try:
        fcntl.flock(lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock_handle.close()
        return SelfUpdateResult(
            status="deferred",
            from_version=package.version or __version__,
            to_version=None,
            detail="Another self-update is already running.",
        )
    try:
        return _run_self_update_locked(package, force=force, report=report)
    finally:
        fcntl.flock(lock_handle, fcntl.LOCK_UN)
        lock_handle.close()


def _run_self_update_locked(
    package: RuntimePackage, *, force: bool, report
) -> SelfUpdateResult:
    channel = installed_channel(package)
    manifest = _fetch_manifest(channel)
    current = _parse_version(package.version or __version__)
    latest = _parse_version(str(manifest.get("version", "")))
    if latest is None:
        raise SelfUpdateError("Update manifest has no valid version.")

    layout_version = int(manifest.get("layout_version", 1))
    if layout_version > SUPPORTED_LAYOUT_VERSION:
        return SelfUpdateResult(
            status="blocked",
            from_version=str(current),
            to_version=str(latest),
            detail=(
                f"Release uses package layout {layout_version}, newer than this "
                f"updater understands ({SUPPORTED_LAYOUT_VERSION}). Reinstall "
                "from the desktop app or install.sh."
            ),
        )

    if current is not None and latest <= current:
        _write_update_check_cache(
            UpdateCheck(
                current_version=str(current),
                latest_version=str(latest),
                channel=channel,
                update_available=False,
                update_required=False,
            )
        )
        return SelfUpdateResult(
            status="up-to-date",
            from_version=str(current),
            to_version=str(latest),
        )

    if not force and _voice_session_active():
        return SelfUpdateResult(
            status="deferred",
            from_version=str(current),
            to_version=str(latest),
            detail="A voice session is active; re-run with --force to update now.",
        )

    target = package.target
    targets = manifest.get("targets") or {}
    target_entry = targets.get(target)
    if not isinstance(target_entry, dict):
        raise SelfUpdateError(f"Manifest has no artifact for target {target!r}.")

    report(f"Downloading {latest} for {target}...")
    release_dir = _download_and_extract(
        url=str(target_entry.get("url", "")),
        sha256=str(target_entry.get("sha256", "")),
        version=str(latest),
        target=target,
        report=report,
    )
    _validate_release_dir(release_dir)

    old_root = package.root.resolve()
    report(f"Activating {release_dir.name}...")
    _point_symlink(STANDALONE_PREVIOUS_DIR, old_root)
    _point_symlink(STANDALONE_CURRENT_DIR, release_dir)

    new_launcher = STANDALONE_CURRENT_DIR / "bin" / "openbase-coder"
    if _post_flip(new_launcher, old_root=old_root, new_root=release_dir, report=report):
        _refresh_backend_binaries(report)
        _prune_releases()
        _write_update_check_cache(
            UpdateCheck(
                current_version=str(latest),
                latest_version=str(latest),
                channel=channel,
                update_available=False,
                update_required=False,
            )
        )
        return SelfUpdateResult(
            status="updated", from_version=str(current), to_version=str(latest)
        )

    report("Update failed health checks; rolling back...")
    _point_symlink(STANDALONE_CURRENT_DIR, old_root)
    old_launcher = STANDALONE_CURRENT_DIR / "bin" / "openbase-coder"
    _run_launcher(old_launcher, ["services", "install"], report=report)
    return SelfUpdateResult(
        status="rolled-back",
        from_version=str(current),
        to_version=str(latest),
        detail=f"{latest} failed post-update health checks; restored {current}.",
    )


def result_payload(result: SelfUpdateResult) -> dict:
    return asdict(result)


# --- feed ---------------------------------------------------------------


def _fetch_manifest(channel: str) -> dict:
    if channel == "beta":
        manifest_url, signature_url = _beta_manifest_urls()
    else:
        manifest_url = STABLE_MANIFEST_URL
        signature_url = STABLE_MANIFEST_URL + ".sig"

    manifest_bytes = _http_get(manifest_url)
    _verify_manifest_signature(manifest_bytes, signature_url)
    try:
        manifest = json.loads(manifest_bytes)
    except json.JSONDecodeError as exc:
        raise SelfUpdateError(f"Update manifest is not valid JSON: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SelfUpdateError("Update manifest must be a JSON object.")
    schema = int(manifest.get("manifest_schema", 0))
    if schema > MANIFEST_SCHEMA:
        raise SelfUpdateError(
            f"Update manifest schema {schema} is newer than this updater "
            f"understands ({MANIFEST_SCHEMA})."
        )
    return manifest


def _beta_manifest_urls() -> tuple[str, str]:
    releases = json.loads(_http_get(RELEASES_API_URL))
    for release in releases:
        if release.get("draft"):
            continue
        assets = {
            asset.get("name"): asset.get("browser_download_url")
            for asset in release.get("assets", [])
        }
        if MANIFEST_ASSET_NAME in assets:
            return (
                assets[MANIFEST_ASSET_NAME],
                assets.get(MANIFEST_SIGNATURE_ASSET_NAME, ""),
            )
    raise SelfUpdateError("No release with an update manifest was found.")


def _verify_manifest_signature(manifest_bytes: bytes, signature_url: str) -> None:
    if not UPDATE_MANIFEST_PUBLIC_KEY_B64:
        logger.warning(
            "Update manifest signature verification skipped: no public key "
            "is configured in this build."
        )
        return
    if not signature_url:
        raise SelfUpdateError("Signed updates required but no signature asset found.")
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    signature = base64.b64decode(_http_get(signature_url))
    public_key = Ed25519PublicKey.from_public_bytes(
        base64.b64decode(UPDATE_MANIFEST_PUBLIC_KEY_B64)
    )
    try:
        public_key.verify(signature, manifest_bytes)
    except InvalidSignature as exc:
        raise SelfUpdateError("Update manifest signature verification failed.") from exc


def _http_get(url: str) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            return response.read()
    except urllib.error.URLError as exc:
        raise SelfUpdateError(f"Could not fetch {url}: {exc}") from exc


# --- download / extract / validate ---------------------------------------


def _download_and_extract(
    *, url: str, sha256: str, version: str, target: str, report
) -> Path:
    if not url:
        raise SelfUpdateError("Manifest target entry has no download URL.")
    release_dir = STANDALONE_RELEASES_DIR / f"{version}-{target}"
    STANDALONE_RELEASES_DIR.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=STANDALONE_RELEASES_DIR) as tmp:
        tmp_dir = Path(tmp)
        archive_path = tmp_dir / "package.tar.gz"
        with urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            with archive_path.open("wb") as handle:
                shutil.copyfileobj(response, handle)

        digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        if not sha256 or digest != sha256:
            raise SelfUpdateError(
                f"Downloaded artifact checksum mismatch (expected {sha256}, "
                f"got {digest})."
            )

        extract_dir = tmp_dir / "extract"
        extract_dir.mkdir()
        with tarfile.open(archive_path, "r:gz") as archive:
            archive.extractall(extract_dir, filter="data")
        package_root = _find_package_root(extract_dir)
        if package_root is None:
            raise SelfUpdateError(
                f"Archive does not contain {PACKAGE_METADATA_FILENAME}."
            )

        if release_dir.exists():
            shutil.rmtree(release_dir)
        package_root.rename(release_dir)
    report(f"Extracted to {release_dir}")
    return release_dir


def _find_package_root(extract_dir: Path) -> Path | None:
    if (extract_dir / PACKAGE_METADATA_FILENAME).is_file():
        return extract_dir
    for child in sorted(extract_dir.iterdir()):
        if child.is_dir() and (child / PACKAGE_METADATA_FILENAME).is_file():
            return child
    return None


def _validate_release_dir(release_dir: Path) -> None:
    launcher = release_dir / "bin" / "openbase-coder"
    required = (
        release_dir / PACKAGE_METADATA_FILENAME,
        launcher,
        release_dir / "bin" / "livekit-server",
    )
    for path in required:
        if not path.is_file():
            raise SelfUpdateError(f"Downloaded package is missing {path.name}.")
    smoke = subprocess.run(
        [str(launcher), "--version"],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if smoke.returncode != 0:
        raise SelfUpdateError(
            f"Downloaded package failed to run: {smoke.stderr.strip()[:500]}"
        )


# --- flip / post-flip / rollback ------------------------------------------


def _point_symlink(link: Path, destination: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp_link = link.with_name(f"{link.name}.tmp-{os.getpid()}")
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    tmp_link.symlink_to(destination)
    os.replace(tmp_link, link)


def _post_flip(new_launcher: Path, *, old_root: Path, new_root: Path, report) -> bool:
    if not _run_launcher(new_launcher, ["services", "install"], report=report):
        return False
    if _bundled_python_changed(old_root, new_root):
        report("Bundled Python changed; rebuilding the plugin site...")
        _run_launcher(new_launcher, ["plugins", "rebuild-site"], report=report)
    return True


def _run_launcher(launcher: Path, args: list[str], *, report) -> bool:
    completed = subprocess.run(
        [str(launcher), *args],
        check=False,
        capture_output=True,
        text=True,
        timeout=600,
    )
    if completed.returncode != 0:
        report(
            f"`openbase-coder {' '.join(args)}` failed: "
            f"{(completed.stderr or completed.stdout).strip()[:500]}"
        )
        return False
    return True


def _bundled_python_changed(old_root: Path, new_root: Path) -> bool:
    old = str(_read_package_metadata(old_root).get("pythonVersion", ""))
    new = str(_read_package_metadata(new_root).get("pythonVersion", ""))
    return bool(old and new) and old.rsplit(".", 1)[0] != new.rsplit(".", 1)[0]


def _refresh_backend_binaries(report) -> None:
    try:
        refreshed = refresh_openbase_bin_codex()
    except Exception as exc:  # network best-effort; never fail the update
        report(f"Could not refresh the codex CLI: {exc}")
        return
    if refreshed:
        report("Refreshed the codex CLI in ~/.openbase/bin.")


def _prune_releases() -> None:
    keep: set[Path] = set()
    for link in (STANDALONE_CURRENT_DIR, STANDALONE_PREVIOUS_DIR):
        if link.is_symlink():
            keep.add(link.resolve())
    if not STANDALONE_RELEASES_DIR.is_dir():
        return
    releases = sorted(
        (child for child in STANDALONE_RELEASES_DIR.iterdir() if child.is_dir()),
        key=lambda child: child.stat().st_mtime,
        reverse=True,
    )
    for child in releases[KEEP_RELEASES:]:
        if child.resolve() not in keep:
            shutil.rmtree(child)


# --- helpers ---------------------------------------------------------------


def _voice_session_active() -> bool:
    from openbase_coder_cli.livekit_announcer import active_voice_room_exists

    try:
        return asyncio.run(active_voice_room_exists())
    except Exception:  # indeterminate (no creds / livekit down) => no session
        return False


def _parse_version(value: str) -> Version | None:
    try:
        return Version(value)
    except (InvalidVersion, TypeError):
        return None


def _below_minimum(current: Version | None, manifest: dict) -> bool:
    minimum = _parse_version(str(manifest.get("min_supported_version", "")))
    cloud_minimum = _parse_version(str(_cloud_minimum_version() or ""))
    for bound in (minimum, cloud_minimum):
        if bound is not None and current is not None and current < bound:
            return True
    return False


def _cloud_minimum_version() -> str | None:
    from openbase_coder_cli.services.onboarding import read_onboarding_cache

    cache = read_onboarding_cache()
    for payload in cache.values():
        if isinstance(payload, dict) and payload.get("minimum_cli_version"):
            return str(payload["minimum_cli_version"])
    return None


def _read_package_metadata(root: Path) -> dict:
    try:
        payload = json.loads(
            (root / PACKAGE_METADATA_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_update_check_cache() -> dict:
    try:
        payload = json.loads(UPDATE_CHECK_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_update_check_cache(check: UpdateCheck) -> None:
    UPDATE_CHECK_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPDATE_CHECK_CACHE_PATH.write_text(
        json.dumps(asdict(check), indent=2) + "\n", encoding="utf-8"
    )
