from __future__ import annotations

from pathlib import Path

from openbase_coder_cli import onboarding_reminder
from openbase_coder_cli.onboarding_reminder import (
    ONBOARDING_REMINDER,
    append_onboarding_reminder,
    onboarding_skill_read,
)


def _use_marker(monkeypatch, marker: Path) -> None:
    monkeypatch.setattr(
        onboarding_reminder, "ONBOARDING_SKILL_READ_MARKER_PATH", marker
    )


def test_reminder_appended_when_marker_missing(tmp_path: Path, monkeypatch) -> None:
    _use_marker(monkeypatch, tmp_path / "onboarding-skill-read")

    assert not onboarding_skill_read()
    result = append_onboarding_reminder("Start my day")
    assert result.startswith(ONBOARDING_REMINDER)
    assert result.endswith("Start my day")
    assert ONBOARDING_REMINDER in result
    assert "To remove this note from future messages" in result
    assert "read and follow the openbase-onboarding skill now" in result
    assert "even if the user skips" in result
    assert "user's current request is primary" in result
    assert "Never replace" in result


def test_no_reminder_when_marker_exists(tmp_path: Path, monkeypatch) -> None:
    marker = tmp_path / "onboarding-skill-read"
    marker.touch()
    _use_marker(monkeypatch, marker)

    assert onboarding_skill_read()
    assert append_onboarding_reminder("Start my day") == "Start my day"


def test_reminder_not_duplicated(tmp_path: Path, monkeypatch) -> None:
    _use_marker(monkeypatch, tmp_path / "onboarding-skill-read")

    once = append_onboarding_reminder("Start my day")
    assert append_onboarding_reminder(once) == once
