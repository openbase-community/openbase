"""Preserve installed plugins if a runtime upgrade cannot migrate their site."""

from __future__ import annotations

import shutil
import tempfile
from contextlib import nullcontext
from pathlib import Path


def restore_plugin_site(site: Path, backup: Path) -> None:
    if site.exists():
        shutil.rmtree(site)
    if backup.exists():
        shutil.copytree(backup, site, symlinks=True)


def migrate_plugin_site(
    site: Path,
    launcher: Path,
    *,
    run_launcher,
    report,
    durable_backup: Path | None = None,
) -> bool:
    site.parent.mkdir(parents=True, exist_ok=True)
    backup_context = (
        nullcontext(durable_backup)
        if durable_backup is not None
        else tempfile.TemporaryDirectory(prefix="update-plugins-", dir=site.parent)
    )
    with backup_context as temp:
        backup = Path(temp) / "site"
        if durable_backup is None and site.exists():
            shutil.copytree(site, backup, symlinks=True)
        committed = False
        try:
            if not run_launcher(launcher, ["plugins", "rebuild-site"], report=report):
                return False
            if not run_launcher(launcher, ["services", "install"], report=report):
                return False
            committed = True
            return True
        finally:
            if not committed:
                restore_plugin_site(site, backup)
