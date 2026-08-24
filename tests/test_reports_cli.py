from __future__ import annotations

import importlib
import os
import time
from pathlib import Path

from click.testing import CliRunner

from openbase_coder_cli.cli import main

reports_service = importlib.import_module("openbase_coder_cli.reports_service")


def _write_report(project: Path, relative_path: str, content: str) -> Path:
    path = project / ".reports" / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_reports_list_uses_shared_report_discovery(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    report = _write_report(project, "2026-06-30-summary.md", "# Summary")
    os.utime(report, (1_767_225_600, 1_767_225_600))

    monkeypatch.setattr(
        reports_service,
        "_get_recent_projects",
        lambda: [{"path": str(project), "name": "Project"}],
    )
    monkeypatch.setattr(reports_service, "CODEX_HOME_DIR", tmp_path / "missing-codex")
    monkeypatch.setattr(
        reports_service, "HOME_REPORTS_PROJECT_DIR", tmp_path / "missing-home"
    )

    result = CliRunner().invoke(main, ["reports", "list", "--repo", "project"])

    assert result.exit_code == 0, result.output
    assert "2026-06-30-summary.md" in result.output
    assert "filename-date=2026-06-30" in result.output
    assert "Date filters use filesystem modified time" in result.output


def test_reports_list_filters_by_modified_date(monkeypatch, tmp_path: Path) -> None:
    project = tmp_path / "project"
    old_report = _write_report(project, "old.md", "# Old")
    new_report = _write_report(project, "new.md", "# New")
    # Midnight-UTC timestamps: 2025-12-31 and 2026-01-01. Pin the process
    # timezone to UTC so the modified-date filter is deterministic.
    os.utime(old_report, (1_767_139_200, 1_767_139_200))
    os.utime(new_report, (1_767_225_600, 1_767_225_600))

    monkeypatch.setattr(
        reports_service,
        "_get_recent_projects",
        lambda: [{"path": str(project), "name": "Project"}],
    )
    monkeypatch.setattr(reports_service, "CODEX_HOME_DIR", tmp_path / "missing-codex")
    monkeypatch.setattr(
        reports_service, "HOME_REPORTS_PROJECT_DIR", tmp_path / "missing-home"
    )

    original_tz = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        result = CliRunner().invoke(
            main,
            ["reports", "list", "--when", "2026-01-01", "--json"],
        )
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()

    assert result.exit_code == 0, result.output
    assert "new.md" in result.output
    assert "old.md" not in result.output


def test_reports_read_resolves_absolute_report_path(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    report = _write_report(project, "nested/summary.md", "# Summary\n\nBody")

    monkeypatch.setattr(
        reports_service,
        "_get_recent_projects",
        lambda: [{"path": str(project), "name": "Project"}],
    )
    monkeypatch.setattr(reports_service, "CODEX_HOME_DIR", tmp_path / "missing-codex")
    monkeypatch.setattr(
        reports_service, "HOME_REPORTS_PROJECT_DIR", tmp_path / "missing-home"
    )

    result = CliRunner().invoke(main, ["reports", "read", str(report)])

    assert result.exit_code == 0, result.output
    assert result.output == "# Summary\n\nBody\n"


def test_reports_show_reports_ambiguous_relative_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    project_a = tmp_path / "a"
    project_b = tmp_path / "b"
    _write_report(project_a, "summary.md", "# A")
    _write_report(project_b, "summary.md", "# B")

    monkeypatch.setattr(
        reports_service,
        "_get_recent_projects",
        lambda: [
            {"path": str(project_a), "name": "A"},
            {"path": str(project_b), "name": "B"},
        ],
    )
    monkeypatch.setattr(reports_service, "CODEX_HOME_DIR", tmp_path / "missing-codex")
    monkeypatch.setattr(
        reports_service, "HOME_REPORTS_PROJECT_DIR", tmp_path / "missing-home"
    )

    result = CliRunner().invoke(main, ["reports", "show", "summary.md"])

    assert result.exit_code != 0
    assert "ambiguous" in result.output
