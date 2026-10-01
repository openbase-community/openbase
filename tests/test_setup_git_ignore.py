from __future__ import annotations

import subprocess
from pathlib import Path

from openbase_coder_cli.cli.setup import git_ignore


def _use_global_config(monkeypatch, tmp_path: Path, excludes: Path | None) -> None:
    config = tmp_path / "gitconfig"
    config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    if excludes is not None:
        subprocess.run(
            ["git", "config", "--global", "core.excludesFile", str(excludes)],
            check=True,
        )


def test_ensure_entries_appends_missing_entries_once(monkeypatch, tmp_path) -> None:
    excludes = tmp_path / "ignore"
    excludes.write_text(".DS_Store\n.reports/")  # no trailing newline
    _use_global_config(monkeypatch, tmp_path, excludes)

    path, added = git_ignore.ensure_global_git_ignore_entries()
    assert path == excludes
    assert added == [".triggers/"]
    assert excludes.read_text() == ".DS_Store\n.reports/\n.triggers/\n"

    assert git_ignore.ensure_global_git_ignore_entries() == (excludes, [])
    assert excludes.read_text() == ".DS_Store\n.reports/\n.triggers/\n"


def test_ensure_entries_creates_gits_default_ignore_file(monkeypatch, tmp_path) -> None:
    _use_global_config(monkeypatch, tmp_path, None)

    path, added = git_ignore.ensure_global_git_ignore_entries()
    assert path == tmp_path / "xdg" / "git" / "ignore"
    assert added == [".triggers/"]
    assert path.read_text() == ".triggers/\n"


def test_setup_step_warns_instead_of_failing_on_unwritable_ignore(
    monkeypatch, capsys
) -> None:
    def unwritable(entries=git_ignore.GLOBAL_GIT_IGNORE_ENTRIES):
        raise PermissionError("read-only ignore file")

    monkeypatch.setattr(git_ignore, "ensure_global_git_ignore_entries", unwritable)
    git_ignore.ensure_global_git_ignore()
    text = capsys.readouterr().out
    assert "Could not update the global Git ignore" in text
    assert ".triggers/" in text


def test_setup_step_reports_added_entries(monkeypatch, tmp_path, capsys) -> None:
    excludes = tmp_path / "ignore"
    _use_global_config(monkeypatch, tmp_path, excludes)
    git_ignore.ensure_global_git_ignore()
    assert (
        f"Added .triggers/ to the global Git ignore at {excludes}"
        in capsys.readouterr().out
    )
