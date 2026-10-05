from __future__ import annotations

from pathlib import Path

from openbase_coder_cli import routine_projects
from openbase_coder_cli.routine_projects import (
    annotate_routines_payload,
    filter_routines_by_project,
    tracked_project_for_directory,
)


def _projects(tmp_path: Path) -> list[dict]:
    return [
        {"path": str(tmp_path / "code")},
        {"path": str(tmp_path / "code" / "coder-workspace")},
        {"path": str(tmp_path / "elsewhere")},
    ]


def test_deepest_tracked_project_wins(tmp_path: Path) -> None:
    for name in ("code/coder-workspace/cli", "elsewhere"):
        (tmp_path / name).mkdir(parents=True)
    projects = _projects(tmp_path)

    inside_workspace = str(tmp_path / "code" / "coder-workspace" / "cli")
    assert tracked_project_for_directory(inside_workspace, projects) == str(
        tmp_path / "code" / "coder-workspace"
    )
    # The parent folder is itself a project: a loop rooted there belongs to it.
    assert tracked_project_for_directory(str(tmp_path / "code"), projects) == str(
        tmp_path / "code"
    )


def test_prefix_match_is_path_segment_aware(tmp_path: Path) -> None:
    (tmp_path / "code-other").mkdir()
    projects = [{"path": str(tmp_path / "code")}]
    # "code-other" starts with "code" as a string but is not inside it.
    assert tracked_project_for_directory(str(tmp_path / "code-other"), projects) is None


def test_untracked_or_missing_cwd_yields_none(tmp_path: Path) -> None:
    projects = _projects(tmp_path)
    assert tracked_project_for_directory(None, projects) is None
    assert tracked_project_for_directory("", projects) is None
    assert tracked_project_for_directory("/definitely/not/tracked", projects) is None


def test_annotate_and_filter_skip_peer_items(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "code").mkdir()
    monkeypatch.setattr(
        routine_projects,
        "get_recent_projects",
        lambda: [{"path": str(tmp_path / "code")}],
    )
    payload = {
        "count": 3,
        "routines": [
            {"name": "local", "cwd": str(tmp_path / "code" / "sub")},
            {"name": "homeless", "cwd": None},
            # Peer loops are stamped by their own device; leave them untouched.
            {
                "name": "peer",
                "cwd": "/x",
                "origin_host": "mini",
                "projectPath": "/mini/x",
            },
        ],
    }

    annotated = annotate_routines_payload(payload)
    by_name = {r["name"]: r for r in annotated["routines"]}
    assert by_name["local"]["projectPath"] == str(tmp_path / "code")
    assert by_name["homeless"]["projectPath"] is None
    assert by_name["peer"]["projectPath"] == "/mini/x"

    filtered = filter_routines_by_project(annotated, str(tmp_path / "code"))
    assert [r["name"] for r in filtered["routines"]] == ["local"]
    assert filtered["count"] == 1


def test_annotate_detail_payload(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "code").mkdir()
    monkeypatch.setattr(
        routine_projects,
        "get_recent_projects",
        lambda: [{"path": str(tmp_path / "code")}],
    )
    detail = {"routine": {"name": "x", "cwd": str(tmp_path / "code")}}
    assert annotate_routines_payload(detail)["routine"]["projectPath"] == str(
        tmp_path / "code"
    )
