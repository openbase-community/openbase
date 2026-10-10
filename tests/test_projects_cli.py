"""Project creation must create a usable cwd and register it immediately."""

import json
from pathlib import Path

from click.testing import CliRunner

from openbase_coder_cli.cli.projects import projects as command
from openbase_coder_cli.thread_sync import projects


def test_create_registers_and_restores_without_overwriting(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())
    path = tmp_path / "nested" / "app with spaces"
    runner = CliRunner()
    result = runner.invoke(command, ["create", str(path)])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(path)
    assert path.is_dir()
    assert projects.get_recent_projects() == [{"path": str(path)}]
    existing = path / "README.md"
    existing.write_text("keep me")
    projects.remove_project(str(path))
    result = runner.invoke(command, ["create", str(path)])
    assert result.exit_code == 0
    assert existing.read_text() == "keep me"
    assert json.loads(runner.invoke(command, ["list"]).output) == [{"path": str(path)}]


def test_create_rejects_file_and_ignored_path(tmp_path, monkeypatch):
    monkeypatch.setattr(projects, "PROJECTS_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", (tmp_path / "system",))
    path = tmp_path / "file"
    path.write_text("keep")
    runner = CliRunner()
    assert runner.invoke(command, ["create", str(path)]).exit_code != 0
    forbidden = tmp_path / "system" / "app"
    assert runner.invoke(command, ["create", str(forbidden)]).exit_code != 0
    assert not forbidden.exists()
    assert runner.invoke(command, ["create", str(Path.home())]).exit_code != 0
    assert not projects.PROJECTS_FILE.exists()


def test_add_normalizes_multi_workspace_and_requires_existing_folder(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(projects, "PROJECTS_FILE", tmp_path / "projects.json")
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())
    workspace = tmp_path / "workspace"
    child = workspace / "cli"
    child.mkdir(parents=True)
    (workspace / "multi.json").write_text(
        json.dumps({"repos": [{"name": "cli", "url": "https://example.test/cli"}]})
    )
    runner = CliRunner()
    result = runner.invoke(command, ["add", str(child)])
    assert result.exit_code == 0
    assert result.output.strip() == str(workspace)
    assert projects.get_recent_projects() == [{"path": str(workspace)}]
    missing = tmp_path / "missing"
    assert runner.invoke(command, ["add", str(missing)]).exit_code != 0
    assert not missing.exists()
