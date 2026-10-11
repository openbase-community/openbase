"""Ask macOS for the protected-folder grants before an agent needs them.

macOS attributes a folder consent (Desktop, Documents, Downloads) to the
launchd job's code identity, the signed ``Openbase Services`` launcher every
service runs through. The first time *any* service process touches one of
those folders, the whole process family blocks in the kernel until the dialog
is answered: a Claude Code process spawned into ``~/Desktop/<project>`` hangs
in ``getcwd()`` and the user's first turn dies a minute later (VM2,
2026-10-11). Listing the three folders once when the local API starts raises
the dialogs at setup time instead, while the person is at the Mac and the
onboarding is on screen, and never in the middle of a call.

The listings run sequentially on one daemon thread, so the prompts appear one
after another rather than stacked, and a thread blocked on an unanswered
prompt costs nothing and ends when the prompt is answered.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

PROTECTED_HOME_FOLDERS = ("Desktop", "Documents", "Downloads")
#: Set to ``0`` to skip the start-up request (tests, CI, headless hosts).
PRELOAD_ENV = "OPENBASE_FOLDER_ACCESS_PRELOAD"


def protected_home_folders(home: Path | None = None) -> list[Path]:
    home = home or Path.home()
    return [home / name for name in PROTECTED_HOME_FOLDERS]


def request_protected_folder_access(folders: list[Path] | None = None) -> list[Path]:
    """List each protected folder in turn; returns the folders that answered.

    A listing that raises (folder missing, access denied) is recorded and
    skipped: a denied grant is the user's decision and surfaces later as the
    agent's own error, not here.
    """
    granted: list[Path] = []
    for folder in folders if folders is not None else protected_home_folders():
        try:
            os.listdir(folder)
        except OSError as exc:
            logger.info(
                "folder_access stage=unavailable folder=%s error=%s", folder, exc
            )
            continue
        granted.append(folder)
    logger.info(
        "folder_access stage=requested granted=%s",
        ",".join(folder.name for folder in granted) or "-",
    )
    return granted


def start_protected_folder_access_request() -> threading.Thread | None:
    """Kick off the request in the background on macOS; no-op elsewhere."""
    if sys.platform != "darwin" or os.environ.get(PRELOAD_ENV, "1") == "0":
        return None
    thread = threading.Thread(
        target=request_protected_folder_access,
        name="openbase-folder-access-request",
        daemon=True,
    )
    thread.start()
    return thread
