from __future__ import annotations

from pathlib import Path

from openbase_coder_cli import folder_access


def test_request_lists_each_folder_and_skips_missing(tmp_path: Path) -> None:
    present = tmp_path / "Desktop"
    present.mkdir()
    granted = folder_access.request_protected_folder_access(
        [present, tmp_path / "Documents"]
    )
    assert granted == [present]


def test_protected_home_folders_are_the_three_tcc_folders(tmp_path: Path) -> None:
    assert [p.name for p in folder_access.protected_home_folders(tmp_path)] == [
        "Desktop",
        "Documents",
        "Downloads",
    ]


def test_start_is_a_no_op_when_disabled(monkeypatch) -> None:
    monkeypatch.setenv(folder_access.PRELOAD_ENV, "0")
    assert folder_access.start_protected_folder_access_request() is None


def test_start_is_a_no_op_off_macos(monkeypatch) -> None:
    monkeypatch.delenv(folder_access.PRELOAD_ENV, raising=False)
    monkeypatch.setattr(folder_access.sys, "platform", "linux")
    assert folder_access.start_protected_folder_access_request() is None
