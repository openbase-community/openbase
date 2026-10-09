"""A named project resolves to its directory, never to some root.

Regression (2026-10-09, staging overnight QA BUG 15): asked to "Start a Super
Agent in tic-tac-toe to read .overnight-qa/game/game.js", the dispatcher
started the agent in /data/workspace instead of /data/workspace/tic-tac-toe.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from openbase_coder_cli import dispatcher_instructions as instructions
from openbase_coder_cli import project_resolution
from openbase_coder_cli.cli.project_dir import project_dir
from openbase_coder_cli.project_resolution import resolve_project_dir


def _workspace(tmp_path):
    root = tmp_path / "workspace"
    for name in ("tic-tac-toe", "Maple_App", ".hidden"):
        (root / name).mkdir(parents=True)
    (root / "notes.txt").write_text("not a project", encoding="utf-8")
    return root


def test_named_project_resolves_to_its_directory_under_the_root(tmp_path) -> None:
    root = _workspace(tmp_path)
    for spoken in ("tic-tac-toe", "Tic Tac Toe", "tic_tac_toe", " TIC-tac toe "):
        result = resolve_project_dir(spoken, projects=[], roots=[root])
        assert result.path == str((root / "tic-tac-toe").resolve()), spoken
        assert result.error is None
    assert resolve_project_dir("maple app", projects=[], roots=[root]).path == str((root / "Maple_App").resolve())


def test_project_list_entries_resolve_outside_the_roots(tmp_path) -> None:
    elsewhere = tmp_path / "Projects" / "cedar" / "code" / "cedar-workspace"
    elsewhere.mkdir(parents=True)
    result = resolve_project_dir("cedar workspace", projects=[str(elsewhere), str(tmp_path / "gone")], roots=[])
    assert result.path == str(elsewhere.resolve())


def test_unknown_project_is_reported_with_candidates_not_a_root(tmp_path) -> None:
    root = _workspace(tmp_path)
    result = resolve_project_dir("chess", projects=[], roots=[root])
    assert result.path is None
    assert result.error == "No project named 'chess' on this computer."
    assert str((root / "tic-tac-toe").resolve()) in result.candidates
    assert str(root.resolve()) not in result.candidates
    assert not any(".hidden" in candidate or "notes.txt" in candidate for candidate in result.candidates)


def test_ambiguous_project_asks_instead_of_guessing(tmp_path) -> None:
    first = tmp_path / "a" / "game"
    second = tmp_path / "b" / "game"
    first.mkdir(parents=True)
    second.mkdir(parents=True)
    result = resolve_project_dir("game", projects=[str(first), str(second)], roots=[])
    assert result.path is None
    assert "More than one project" in (result.error or "")
    assert result.candidates == sorted([str(first.resolve()), str(second.resolve())])


def test_absolute_directory_is_used_as_given(tmp_path) -> None:
    root = _workspace(tmp_path)
    assert resolve_project_dir(str(root / "tic-tac-toe"), projects=[], roots=[]).path == str((root / "tic-tac-toe").resolve())


def test_symlink_aliases_match_without_creating_duplicate_candidates(tmp_path) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    target = root / "actual-project"
    target.mkdir()
    alias = root / "friendly-name"
    alias.symlink_to(target, target_is_directory=True)

    for name in ("actual project", "friendly name"):
        result = resolve_project_dir(name, projects=[str(target)], roots=[root])
        assert result.path == str(target.resolve())
    assert resolve_project_dir("missing", projects=[str(alias)], roots=[root]).candidates == [str(target.resolve())]


def test_unreadable_root_does_not_hide_known_or_other_root_projects(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    unreadable = tmp_path / "unreadable"
    unreadable.mkdir()
    original_iterdir = type(root).iterdir

    def iterdir(path):
        if path == unreadable:
            raise PermissionError("Cannot list directory")
        return original_iterdir(path)

    monkeypatch.setattr(type(root), "iterdir", iterdir)
    for projects, roots in (([str(root / "tic-tac-toe")], [unreadable]), ([], [unreadable, root])):
        result = resolve_project_dir("tic tac toe", projects=projects, roots=roots)
        assert result.path == str((root / "tic-tac-toe").resolve())


def test_default_roots_include_the_configured_projects_dir(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setenv(project_resolution.PROJECTS_DIR_ENV, str(root))
    assert project_resolution.project_roots()[0] == root


def test_project_dir_command_prints_json_and_fails_closed(tmp_path, monkeypatch) -> None:
    root = _workspace(tmp_path)
    monkeypatch.setenv(project_resolution.PROJECTS_DIR_ENV, str(root))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(project_resolution, "known_project_paths", lambda: [])
    runner = CliRunner()

    found = runner.invoke(project_dir, ["tic tac toe", "--json"])
    assert found.exit_code == 0
    assert json.loads(found.output)["path"] == str((root / "tic-tac-toe").resolve())

    missing = runner.invoke(project_dir, ["chess", "--json"])
    assert missing.exit_code == 1
    payload = json.loads(missing.output)
    assert payload["path"] is None
    assert payload["error"] == "No project named 'chess' on this computer."
    assert str((root / "tic-tac-toe").resolve()) in payload["candidates"]

    plain = runner.invoke(project_dir, ["chess"])
    assert plain.exit_code != 0
    assert "No project named 'chess'" in plain.output


def test_dispatcher_rules_pass_a_named_projects_directory_as_cwd(monkeypatch) -> None:
    monkeypatch.setattr(instructions, "canonical_dispatcher_skill", lambda: "Canonical procedure.")
    rules = " ".join(instructions.with_dispatcher_rules("Dispatcher policy.").split())
    assert 'openbase-coder project-dir "<name>" --json' in rules
    assert "pass the returned `path` as the agent's `cwd`" in rules
    assert "Never default to your own directory for a named project" in rules
    assert "do not start the agent anywhere else" in rules
