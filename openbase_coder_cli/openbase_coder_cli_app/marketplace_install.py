"""Pinned marketplace source validation and atomic skill installation."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from openbase_coder_cli.openbase_coder_cli_app.skills import _skills_dir

ARCHIVE_TIMEOUT_SECONDS = 30
MAX_ARCHIVE_BYTES = 12 * 1024 * 1024
MAX_SKILL_FILES = 250
MAX_SKILL_BYTES = 10 * 1024 * 1024
MAX_SINGLE_FILE_BYTES = 3 * 1024 * 1024
SKILL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,79}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
INTEGRITY_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
GITHUB_REPOSITORY_RE = re.compile(
    r"^https://github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)$"
)
INSTALL_METADATA_FILENAME = ".openbase-marketplace.json"
SENSITIVE_FILE_NAMES = {".env", "credentials.json", "id_rsa", "id_ed25519"}
PRIVATE_KEY_MARKERS = (
    b"-----BEGIN " + b"PRIVATE KEY-----",
    b"-----BEGIN RSA " + b"PRIVATE KEY-----",
    b"-----BEGIN OPENSSH " + b"PRIVATE KEY-----",
)


@dataclass(frozen=True, slots=True)
class CatalogSource:
    repository_url: str
    owner: str
    repository: str
    commit: str
    path: str
    integrity: str | None


@dataclass(frozen=True, slots=True)
class SkillFile:
    content: bytes
    executable: bool


class MarketplaceContractError(ValueError):
    """Cloud returned an entry that violates the marketplace contract."""


class MarketplaceInstallError(RuntimeError):
    """A pinned skill archive could not be safely installed."""


def _catalog_source(value: Any) -> CatalogSource | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise MarketplaceContractError("Catalog source must be an object or null.")
    repository_url = value.get("repository_url")
    commit = value.get("commit")
    source_path = value.get("path")
    integrity = value.get("integrity")
    if not all(isinstance(item, str) for item in (repository_url, commit, source_path)):
        raise MarketplaceContractError("Catalog source fields are invalid.")
    match = GITHUB_REPOSITORY_RE.fullmatch(repository_url)
    parsed = urlparse(repository_url)
    if (
        match is None
        or parsed.username
        or parsed.password
        or parsed.port
        or parsed.query
        or parsed.fragment
    ):
        raise MarketplaceContractError("Catalog source repository is not allowed.")
    if not COMMIT_RE.fullmatch(commit):
        raise MarketplaceContractError("Catalog source commit must be immutable.")
    path = _normalized_source_path(source_path)
    if integrity is not None and (
        not isinstance(integrity, str) or INTEGRITY_RE.fullmatch(integrity) is None
    ):
        raise MarketplaceContractError("Catalog source integrity is invalid.")
    return CatalogSource(
        repository_url=repository_url,
        owner=match.group(1),
        repository=match.group(2),
        commit=commit,
        path=path,
        integrity=integrity,
    )


def _normalized_source_path(value: str) -> str:
    if value == ".":
        return value
    if not value or "\\" in value or value.startswith("/") or value.endswith("/"):
        raise MarketplaceContractError("Catalog source path is invalid.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise MarketplaceContractError("Catalog source path is invalid.")
    return "/".join(parts)


def _download_skill_files(source: CatalogSource) -> dict[str, SkillFile]:
    url = (
        f"https://codeload.github.com/{quote(source.owner, safe='')}/"
        f"{quote(source.repository, safe='')}/tar.gz/{source.commit}"
    )
    chunks: list[bytes] = []
    size = 0
    with httpx.stream(
        "GET",
        url,
        timeout=ARCHIVE_TIMEOUT_SECONDS,
        follow_redirects=False,
    ) as response:
        response.raise_for_status()
        if response.url.host != "codeload.github.com":
            raise MarketplaceInstallError("Skill archive came from an unexpected host.")
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_ARCHIVE_BYTES:
                raise MarketplaceInstallError("Skill archive is too large.")
            chunks.append(chunk)
    return _skill_files_from_archive(b"".join(chunks), source)


def _skill_files_from_archive(
    archive: bytes,
    source: CatalogSource,
) -> dict[str, SkillFile]:
    selected: dict[str, SkillFile] = {}
    selected_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = bundle.getmembers()
        top_levels = {
            PurePosixPath(member.name).parts[0]
            for member in members
            if PurePosixPath(member.name).parts
        }
        if len(top_levels) != 1:
            raise MarketplaceInstallError("Skill archive has an invalid root.")
        root = next(iter(top_levels))
        prefix = PurePosixPath(root)
        if source.path != ".":
            prefix /= source.path
        prefix_parts = prefix.parts

        for member in members:
            member_path = PurePosixPath(member.name)
            if member_path.parts[: len(prefix_parts)] != prefix_parts:
                continue
            relative_parts = member_path.parts[len(prefix_parts) :]
            if not relative_parts or member.isdir():
                continue
            if any(
                part in {"", ".", ".."} or "\\" in part
                for part in relative_parts
            ):
                raise MarketplaceInstallError("Skill archive contains an unsafe path.")
            if not member.isfile():
                raise MarketplaceInstallError(
                    "Skill directories may not contain links or special files."
                )
            if member.size > MAX_SINGLE_FILE_BYTES:
                raise MarketplaceInstallError("A skill file is too large.")
            extracted = bundle.extractfile(member)
            if extracted is None:
                raise MarketplaceInstallError("A skill file could not be read.")
            content = extracted.read(MAX_SINGLE_FILE_BYTES + 1)
            if len(content) != member.size:
                raise MarketplaceInstallError("A skill file size is inconsistent.")
            relative = "/".join(relative_parts)
            if relative in selected:
                raise MarketplaceInstallError(
                    "Skill archive contains duplicate file paths."
                )
            selected[relative] = SkillFile(
                content=content,
                executable=bool(member.mode & 0o111),
            )
            selected_bytes += len(content)
            if len(selected) > MAX_SKILL_FILES or selected_bytes > MAX_SKILL_BYTES:
                raise MarketplaceInstallError("Skill contents exceed safe size limits.")
    if not selected:
        raise MarketplaceInstallError("The catalog path is missing from the archive.")
    return selected


def _verify_skill_files(files: dict[str, SkillFile], source: CatalogSource) -> None:
    skill_file = files.get("SKILL.md")
    if skill_file is None or not skill_file.content.strip():
        raise MarketplaceInstallError("Catalog skill does not contain SKILL.md.")
    if source.integrity:
        expected = INTEGRITY_RE.fullmatch(source.integrity)
        if expected is None:
            raise MarketplaceContractError("Catalog source integrity is invalid.")
        actual = hashlib.sha256(skill_file.content).hexdigest()
        if actual != expected.group(1):
            raise MarketplaceInstallError("SKILL.md integrity verification failed.")
    for relative, file in files.items():
        if PurePosixPath(relative).name.lower() in SENSITIVE_FILE_NAMES:
            raise MarketplaceInstallError(
                f"Catalog skill contains a prohibited sensitive file: {relative}."
            )
        if any(marker in file.content for marker in PRIVATE_KEY_MARKERS):
            raise MarketplaceInstallError(
                f"Catalog skill contains private-key material: {relative}."
            )


def _target_conflicts(
    slug: str,
    source: CatalogSource,
    targets: list[str],
) -> list[dict[str, str]]:
    conflicts = []
    for target in targets:
        destination = _skills_dir(None, target) / slug
        if not destination.exists() and not destination.is_symlink():
            continue
        if _installed_source(destination) == _source_identity(source):
            continue
        conflicts.append({"target": target, "reason": "different_existing_skill"})
    return conflicts


def _installed_target_status(
    slug: str,
    source: CatalogSource | None,
    target: str,
) -> str:
    destination = _skills_dir(None, target) / slug
    if not destination.exists() and not destination.is_symlink():
        return "not_installed"
    if source and _installed_source(destination) == _source_identity(source):
        return "installed"
    return "conflict"


def _installed_source(destination: Path) -> dict[str, Any] | None:
    if destination.is_symlink() or not destination.is_dir():
        return None
    metadata_path = destination / INSTALL_METADATA_FILENAME
    if metadata_path.is_symlink() or not metadata_path.is_file():
        return None
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload.get("source") if isinstance(payload, dict) else None


def _source_identity(source: CatalogSource) -> dict[str, Any]:
    return {
        "repository_url": source.repository_url,
        "commit": source.commit,
        "path": source.path,
        "integrity": source.integrity,
    }


def _install_skill_files(
    *,
    slug: str,
    source: CatalogSource,
    entry: dict[str, Any],
    files: dict[str, SkillFile],
    targets: list[str],
) -> list[dict[str, Any]]:
    source_identity = _source_identity(source)
    metadata = json.dumps(
        {
            "schema_version": 1,
            "slug": slug,
            "name": entry.get("name"),
            "source": source_identity,
        },
        indent=2,
        sort_keys=True,
    ).encode("utf-8") + b"\n"
    installed: list[Path] = []
    results: list[dict[str, Any]] = []
    try:
        for target in targets:
            root = _skills_dir(None, target)
            destination = root / slug
            if _installed_source(destination) == source_identity:
                results.append({"target": target, "status": "already_installed"})
                continue
            root.mkdir(parents=True, exist_ok=True)
            temporary = Path(tempfile.mkdtemp(prefix=f".{slug}.install-", dir=root))
            try:
                for relative, skill_file in files.items():
                    file_path = temporary.joinpath(*PurePosixPath(relative).parts)
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_bytes(skill_file.content)
                    file_path.chmod(0o755 if skill_file.executable else 0o644)
                (temporary / INSTALL_METADATA_FILENAME).write_bytes(metadata)
                os.replace(temporary, destination)
            except BaseException:
                shutil.rmtree(temporary, ignore_errors=True)
                raise
            installed.append(destination)
            results.append({"target": target, "status": "installed"})
    except BaseException:
        for destination in installed:
            shutil.rmtree(destination, ignore_errors=True)
        raise
    return results
