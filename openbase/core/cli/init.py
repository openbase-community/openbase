"""Init command for Openbase CLI."""

import json
from pathlib import Path

import click
from vscode_multi.sync import sync

from ..boilersync_manager import (
    init_boilersync_app_package,
    init_boilersync_django_app,
    init_boilersync_react_app,
    setup_boilerplate_dir,
)
from ..git_helpers import (
    create_github_repo,
    create_initial_commit,
    get_github_user,
    init_git_repo,
)


def create_multi_json(current_dir, project_name_kebab, with_frontend):
    multi_json_path = current_dir / "multi.json"
    github_user = get_github_user()
    multi_config = {
        "repos": [
            {"url": "https://github.com/openbase-community/web"},
            {"url": f"https://github.com/{github_user}/{project_name_kebab}-api"},
        ]
    }

    if with_frontend:
        multi_config["repos"] += [
            {"url": f"https://github.com/{github_user}/{project_name_kebab}-react"},
            {"url": "https://github.com/openbase-community/react-shared"},
        ]
        print(multi_config)

    with open(multi_json_path, "w") as f:
        json.dump(multi_config, f, indent=2)

    click.echo(f"Created multi.json at {multi_json_path}")


@click.command()
@click.option(
    "--with-frontend",
    default=True,
    help="Initialize a frontend (React app) as well.",
)
@click.option(
    "--with-github",
    default=False,
    help="Initialize a GitHub repository as well.",
)
def init(with_frontend, with_github):
    """Initialize a new Openbase project in the current directory.

    By default, this will also initialize a frontend (React) app. Use --no-frontend to skip frontend initialization.
    """
    # Set up the boilerplate directory
    click.echo("Setting up boilerplate directory...")
    boilerplate_dir = setup_boilerplate_dir()
    click.echo(f"Using boilerplate directory: {boilerplate_dir}")

    # Run boilersync init with the app-package template
    current_dir = Path.cwd()

    click.echo("Initializing Openbase project...")
    project_name_kebab = current_dir.name
    project_name_snake = project_name_kebab.replace("-", "_")
    app_name = f"{project_name_snake}_app"

    # Initialize app package
    app_package_dir, apps = init_boilersync_app_package(
        boilerplate_dir,
        current_dir,
        project_name_kebab,
        project_name_snake,
        app_name,
    )

    # Initialize Django app
    click.echo("Initializing app repository with template...")
    _ = init_boilersync_django_app(
        app_package_dir,
        project_name_snake,
        app_name,
        apps,
    )

    # Initialize React app
    click.echo("Initializing React app...")
    _ = init_boilersync_react_app(
        app_package_dir,
        project_name_kebab,
        project_name_snake,
    )

    # Create multi.json file
    create_multi_json(current_dir, project_name_kebab, with_frontend)

    # Create the GitHub repo if it doesn't exist
    if with_github:
        click.echo(f"Creating GitHub repository {project_name_kebab} if not exists...")
        create_github_repo(project_name_kebab)

    # Run vscode_multi sync
    click.echo("Syncing multi-repository workspace...")
    sync(ensure_on_same_branch=False)

    # Initialize root git repository
    click.echo("Initializing git repository...")
    init_git_repo(current_dir)

    # Create an initial git commit after syncing
    click.echo("Creating initial git commit...")
    create_initial_commit(current_dir)

    click.echo("Openbase project initialized successfully!")
