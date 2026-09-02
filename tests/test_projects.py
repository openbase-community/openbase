from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from openbase_coder_cli.thread_sync import projects


def test_recent_projects_filters_private_paths(tmp_path: Path, monkeypatch) -> None:
    projects_file = tmp_path / "coder-projects.json"
    private_root = tmp_path / "private"
    var_root = tmp_path / "var"
    visible_project = tmp_path / "real-project"
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", (private_root, var_root))
    projects_file.write_text(
        json.dumps(
            [
                {"path": str(private_root / "var" / "folders" / "pytest" / "project")},
                {"path": str(var_root / "folders" / "pytest" / "project")},
                {"path": str(visible_project)},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)

    assert projects.get_recent_projects() == [{"path": str(visible_project)}]


def test_recent_projects_filters_exact_home_but_keeps_home_children(
    tmp_path: Path, monkeypatch
) -> None:
    projects_file = tmp_path / "coder-projects.json"
    home = tmp_path / "home" / "gabe"
    developer_project = home / "Developer" / "openbase-coder"
    projects_file.write_text(
        json.dumps(
            [
                {"path": str(home)},
                {"path": str(developer_project)},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_EXACT_PROJECT_PATHS", (home,))
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())

    assert projects.get_recent_projects() == [{"path": str(developer_project)}]


def test_track_project_ignores_private_paths(tmp_path: Path, monkeypatch) -> None:
    projects_file = tmp_path / "coder-projects.json"
    private_root = tmp_path / "private"
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", (private_root,))

    projects.track_project(str(private_root / "var" / "folders" / "pytest" / "project"))

    assert not projects_file.exists()


def test_track_project_ignores_exact_home_but_keeps_home_children(
    tmp_path: Path, monkeypatch
) -> None:
    projects_file = tmp_path / "coder-projects.json"
    home = tmp_path / "home" / "gabe"
    developer_project = home / "Developer" / "openbase-coder"
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_EXACT_PROJECT_PATHS", (home,))
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())

    projects.track_project(str(home))

    assert not projects_file.exists()

    projects.track_project(str(developer_project))

    assert projects.get_recent_projects() == [{"path": str(developer_project)}]


def test_thread_directory_inside_multi_workspace_uses_workspace_root(
    tmp_path: Path, monkeypatch
) -> None:
    projects_file = tmp_path / "coder-projects.json"
    workspace = tmp_path / "openbase-coder-workspace"
    repo = workspace / "cli"
    nested = repo / "openbase_coder_cli"
    nested.mkdir(parents=True)
    (workspace / "multi.json").write_text(
        json.dumps({"repos": [{"name": "cli", "url": "https://example.test/cli"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())

    projects.refresh_projects_from_thread_directories([str(nested)])

    assert projects.get_recent_projects() == [{"path": str(workspace)}]


def test_removed_thread_derived_project_stays_hidden_after_refresh(
    tmp_path: Path, monkeypatch
) -> None:
    projects_file = tmp_path / "coder-projects.json"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())

    projects.refresh_projects_from_thread_directories([str(project_dir)])
    assert projects.remove_project(str(project_dir)) is True
    projects.refresh_projects_from_thread_directories([str(project_dir)])

    assert projects.get_recent_projects() == []


def test_recent_projects_translate_foreign_home_paths(
    tmp_path: Path, monkeypatch
) -> None:
    projects_file = tmp_path / "coder-projects.json"
    projects_file.write_text(
        json.dumps(
            [
                {
                    "path": "/Users/source-user/Projects/openbase",
                    "source": "thread",
                },
                {"path": "/tmp/remembered-project", "source": "thread"},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())

    assert projects.get_recent_projects() == [
        {"path": str(Path.home() / "Projects" / "openbase")},
        {"path": "/tmp/remembered-project"},
    ]


def test_remove_project_untracks_path_without_deleting_directory(
    tmp_path: Path, monkeypatch
) -> None:
    projects_file = tmp_path / "coder-projects.json"
    project_dir = tmp_path / "project"
    project_file = project_dir / "README.md"
    other_project = tmp_path / "other-project"
    project_dir.mkdir()
    other_project.mkdir()
    project_file.write_text("still here", encoding="utf-8")
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())

    projects.track_project(str(other_project))
    projects.track_project(str(project_dir))

    assert projects.remove_project(str(project_dir)) is True
    assert project_dir.is_dir()
    assert project_file.read_text(encoding="utf-8") == "still here"
    assert projects.get_recent_projects() == [{"path": str(other_project)}]


def test_remove_project_removes_duplicate_resolved_paths(
    tmp_path: Path, monkeypatch
) -> None:
    projects_file = tmp_path / "coder-projects.json"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    projects_file.write_text(
        json.dumps(
            [
                {"path": str(project_dir)},
                {"path": str(project_dir.resolve())},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)

    assert projects.remove_project(str(project_dir)) is True
    assert projects.get_recent_projects() == []


def test_recent_projects_post_ignores_exact_home_directory(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django
    from rest_framework.test import APIRequestFactory, force_authenticate

    django.setup()

    from openbase_coder_cli.openbase_coder_cli_app import views

    projects_file = tmp_path / "coder-projects.json"
    home = tmp_path / "home" / "gabe"
    home.mkdir(parents=True)
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_EXACT_PROJECT_PATHS", (home,))
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())

    factory = APIRequestFactory()
    request = factory.post("/api/projects/recent/", {"path": str(home)}, format="json")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = views.recent_projects(request)

    assert response.status_code == 201
    assert not projects_file.exists()


def test_recent_projects_delete_untracks_without_requiring_directory(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django
    from rest_framework.test import APIRequestFactory, force_authenticate

    django.setup()

    from openbase_coder_cli.openbase_coder_cli_app import views

    projects_file = tmp_path / "coder-projects.json"
    missing_project = tmp_path / "missing-project"
    other_project = tmp_path / "other-project"
    projects_file.write_text(
        json.dumps(
            [
                {"path": str(missing_project)},
                {"path": str(other_project)},
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", ())

    factory = APIRequestFactory()
    request = factory.delete(
        "/api/projects/recent/",
        {"path": str(missing_project)},
        format="json",
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = views.recent_projects(request)

    assert response.status_code == 200
    assert response.data == {"path": str(missing_project), "removed": True}
    assert projects.get_recent_projects() == [{"path": str(other_project)}]


def test_track_project_ignores_var_paths(tmp_path: Path, monkeypatch) -> None:
    projects_file = tmp_path / "coder-projects.json"
    var_root = tmp_path / "var"
    monkeypatch.setattr(projects, "PROJECTS_FILE", projects_file)
    monkeypatch.setattr(projects, "IGNORED_PROJECT_ROOTS", (var_root,))

    projects.track_project(str(var_root / "folders" / "pytest" / "project"))

    assert not projects_file.exists()


def test_recent_projects_get_returns_paginated_lazy_metadata(monkeypatch) -> None:
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django
    from rest_framework.test import APIRequestFactory, force_authenticate

    django.setup()

    from openbase_coder_cli.openbase_coder_cli_app import projects as project_views

    scheduled: list[list[str]] = []
    monkeypatch.setattr(
        project_views,
        "_get_cached_recent_projects",
        lambda: [
            {"path": "/tmp/project-1"},
            {"path": "/tmp/project-2"},
            {"path": "/tmp/project-3"},
        ],
    )
    monkeypatch.setattr(
        project_views,
        "_schedule_project_metadata_refresh",
        lambda paths: scheduled.append(paths),
    )
    monkeypatch.setattr(project_views, "_cached_metadata", lambda _: None)
    monkeypatch.setattr(project_views, "_worktree_children", lambda _: [])

    factory = APIRequestFactory()
    request = factory.get("/api/projects/recent/?page_size=2")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = project_views.recent_projects(request)

    assert response.status_code == 200
    assert response.data["count"] == 3
    assert response.data["page"] == 1
    assert response.data["page_size"] == 2
    assert response.data["next"] == "/api/projects/recent/?page_size=2&page=2"
    assert response.data["previous"] is None
    assert response.data["projects"] == [
        {
            "path": "/tmp/project-1",
            "git_status": "unknown",
            "stack": None,
            "worktrees": [],
        },
        {
            "path": "/tmp/project-2",
            "git_status": "unknown",
            "stack": None,
            "worktrees": [],
        },
    ]
    assert scheduled == [["/tmp/project-1", "/tmp/project-2"]]


def test_recent_projects_get_derives_multi_worktree_children(
    tmp_path: Path, monkeypatch
) -> None:
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django
    from rest_framework.test import APIRequestFactory, force_authenticate

    django.setup()

    from openbase_coder_cli.openbase_coder_cli_app import projects as project_views

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "multi.json").write_text("{}", encoding="utf-8")
    worktrees = tmp_path / "workspace-worktrees"
    worktree = worktrees / "feature-a"
    worktree.mkdir(parents=True)
    (worktree / "multi.json").write_text("{}", encoding="utf-8")
    ignored = worktrees / "notes"
    ignored.mkdir()

    scheduled: list[list[str]] = []
    monkeypatch.setattr(
        project_views,
        "_get_cached_recent_projects",
        lambda: [{"path": str(workspace)}],
    )
    monkeypatch.setattr(
        project_views,
        "_schedule_project_metadata_refresh",
        lambda paths: scheduled.append(paths),
    )
    monkeypatch.setattr(project_views, "_cached_metadata", lambda _: None)

    factory = APIRequestFactory()
    request = factory.get("/api/projects/recent/")
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = project_views.recent_projects(request)

    assert response.status_code == 200
    assert response.data["projects"] == [
        {
            "path": str(workspace),
            "git_status": "unknown",
            "stack": None,
            "worktrees": [
                {
                    "path": str(worktree.resolve()),
                    "source": "worktree",
                    "git_status": "unknown",
                    "stack": None,
                }
            ],
        }
    ]
    assert scheduled == [[str(workspace), str(worktree.resolve())]]


def test_project_status_returns_fresh_metadata(monkeypatch) -> None:
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django
    from rest_framework.test import APIRequestFactory, force_authenticate

    django.setup()

    from openbase_coder_cli.openbase_coder_cli_app import projects as project_views

    monkeypatch.setattr(
        project_views,
        "_project_metadata",
        lambda path: {"git_status": "clean", "stack": "python", "reports_count": 0},
    )

    factory = APIRequestFactory()
    request = factory.get(
        "/api/projects/status/?path=/tmp/project-1&path=/tmp/project-2"
    )
    force_authenticate(request, user=SimpleNamespace(is_authenticated=True))
    response = project_views.project_status(request)

    assert response.status_code == 200
    assert response.data["projects"] == [
        {
            "path": "/tmp/project-1",
            "git_status": "clean",
            "stack": "python",
            "reports_count": 0,
        },
        {
            "path": "/tmp/project-2",
            "git_status": "clean",
            "stack": "python",
            "reports_count": 0,
        },
    ]


def test_project_metadata_git_status_uses_shared_multi_engine(tmp_path) -> None:
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django

    django.setup()

    from openbase_coder_cli.openbase_coder_cli_app import projects as project_views

    def git(args, cwd):
        subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)

    workspace = tmp_path / "ws"
    sub = workspace / "child"
    for repo in (workspace, sub):
        repo.mkdir(parents=True, exist_ok=True)
        git(["init", "-q"], repo)
        git(["config", "user.email", "t@example.com"], repo)
        git(["config", "user.name", "T"], repo)
    (workspace / "multi.json").write_text(
        '{"repos": [{"name": "child", "url": "https://example.com/child"}]}'
    )
    (sub / "a.txt").write_text("hello\n")
    git(["add", "a.txt"], sub)
    git(["commit", "-q", "-m", "add a"], sub)

    # Sub-repo has no upstream, so the workspace folds to out-of-sync.
    metadata = project_views._project_metadata(str(workspace))
    assert metadata["git_status"] == "out-of-sync"

    # Dirty sub-repo work outranks out-of-sync.
    (sub / "wip.txt").write_text("wip\n")
    assert project_views._project_metadata(str(workspace))["git_status"] == "dirty"


def test_project_metadata_git_status_reports_missing_directory() -> None:
    os.environ.setdefault("OPENBASE_CODER_CLI_SECRET_KEY", "test-secret")
    os.environ.setdefault(
        "DJANGO_SETTINGS_MODULE", "openbase_coder_cli.config.settings"
    )

    import django

    django.setup()

    from openbase_coder_cli.openbase_coder_cli_app import projects as project_views

    metadata = project_views._project_metadata("/tmp/definitely-missing-project")
    assert metadata["git_status"] == "missing"
