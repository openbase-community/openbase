"""Preserve installed plugins if a runtime upgrade cannot migrate their site."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


def migrate_plugin_site(site: Path, launcher: Path, *, run_launcher, report) -> bool:
    site.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="update-plugins-", dir=site.parent) as temp:
        backup = Path(temp) / "site"
        if site.exists():
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
                if site.exists():
                    shutil.rmtree(site)
                if backup.exists():
                    os.replace(backup, site)
