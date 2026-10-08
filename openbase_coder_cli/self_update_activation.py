"""Durable intent and plugin backup for an interrupted runtime activation."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from openbase_coder_cli.self_update_network import SelfUpdateError

JOURNAL_NAME = ".activation.json"


def sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_tree(root: Path) -> None:
    children = list(root.rglob("*"))
    for child in children:
        if child.is_file() and not child.is_symlink():
            with child.open("rb") as handle:
                os.fsync(handle.fileno())
    for child in reversed(children):
        if child.is_dir() and not child.is_symlink():
            sync_directory(child)
    sync_directory(root)


class Activation:
    def __init__(self, directory: Path, data: dict):
        self.directory = directory
        self.data = data

    @property
    def path(self) -> Path:
        return self.directory / JOURNAL_NAME

    @property
    def backup(self) -> Path:
        return self.directory / ".activation-plugins"

    @property
    def old(self) -> Path:
        return Path(self.data["old_root"])

    @property
    def new(self) -> Path:
        return Path(self.data["new_root"])

    @classmethod
    def load(cls, directory: Path) -> Activation | None:
        path = directory / JOURNAL_NAME
        try:
            data = json.loads(path.read_text())
            valid = (
                isinstance(data, dict)
                and data.get("schema_version") == 1
                and data.get("phase") in {"activating", "rollback"}
                and all(
                    isinstance(data.get(k), str) and data[k]
                    for k in (
                        "old_root",
                        "new_root",
                        "from_version",
                        "to_version",
                        "channel",
                    )
                )
                and all(Path(data[k]).is_absolute() for k in ("old_root", "new_root"))
                and isinstance(data.get("migrate_plugins"), bool)
            )
        except FileNotFoundError:
            return None
        except (ValueError, TypeError) as exc:
            raise SelfUpdateError(
                "Cannot read the interrupted activation journal."
            ) from exc
        if not valid:
            raise SelfUpdateError(
                "Unsupported or incomplete activation journal; refusing to overwrite it."
            )
        return cls(directory, data)

    @classmethod
    def begin(
        cls,
        directory: Path,
        *,
        old: Path,
        new: Path,
        current: str,
        latest: str,
        channel: str,
        plugin_site: Path,
        migrate_plugins: bool,
    ) -> Activation:
        if (directory / JOURNAL_NAME).exists():
            raise SelfUpdateError("An interrupted activation must be recovered first.")
        transaction = cls(
            directory,
            {
                "schema_version": 1,
                "phase": "activating",
                "old_root": str(old.resolve()),
                "new_root": str(new.resolve()),
                "from_version": current,
                "to_version": latest,
                "channel": channel,
                "migrate_plugins": migrate_plugins,
            },
        )
        directory.mkdir(parents=True, exist_ok=True)
        # A crash during preparation precedes the journal and cannot have flipped current.
        if transaction.backup.exists():
            shutil.rmtree(transaction.backup)
        transaction.backup.mkdir()
        if migrate_plugins and plugin_site.exists():
            shutil.copytree(plugin_site, transaction.backup / "site", symlinks=True)
        sync_tree(transaction.backup)
        # Persist the validated package before the journal can authorize its
        # activation after a machine restart, not just after process death.
        sync_tree(new)
        sync_directory(new.parent)
        transaction.save()
        return transaction

    def save(self) -> None:
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w") as handle:
            json.dump(self.data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
        sync_directory(self.directory)

    def rollback(self) -> None:
        self.data["phase"] = "rollback"
        self.save()

    def restore_plugins(self, site: Path) -> None:
        if not self.data["migrate_plugins"]:
            return
        if not self.backup.is_dir():
            raise SelfUpdateError(
                "Interrupted activation is missing its plugin backup."
            )
        from openbase_coder_cli.self_update_plugins import restore_plugin_site

        restore_plugin_site(site, self.backup / "site")

    def finish(self) -> None:
        self.path.unlink()
        sync_directory(self.directory)
        shutil.rmtree(self.backup)
